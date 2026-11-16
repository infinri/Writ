//! Key-source contract (M5 J-3, D-045 M5-D6): purposes, key ids, validity
//! windows, purpose-tagged material, the [`KeySource`] trait, the key
//! registry and revocation traits the ledger implements, and adapters that
//! present a [`KeySource`] as the purpose-typed rings used by M3 and M4 code.
//!
//! Types and traits only: no implementation here reads files, the
//! environment or any other ambient source. The file implementation is
//! `writ-keys::FileKeySource`; the ledger implements [`KeyRegistry`].

use super::{
    AcceptanceKeyRing, AcceptanceRecordKey, IngressKey, KeyError, KeyStatus, KeysUnavailable,
    PermitKey, RunnerAttestationKey, RunnerKeyRing,
};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::time::Duration;
use writ_kernel::json::{self, Value};
use writ_kernel::time::Timestamp;

/// Longest key id in bytes.
pub const MAX_KEY_ID_BYTES: usize = 64;

/// Domain-separation prefix of [`MaterialDigest`]. The purpose is not part of
/// the digest, so the same material under two purposes has the same digest
/// and is detected by the registry.
pub const MATERIAL_DIGEST_DOMAIN: &[u8] = b"writ-key-material/1\0";

/// What a key authenticates. A key has exactly one purpose; a key id names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum KeyPurpose {
    /// Ingress seals (D-010).
    Ingress,
    /// Broker permits (SR-07).
    Permit,
    /// Authenticated acceptance records (D-036 item 1).
    AcceptanceRecord,
    /// Runner attestations (D-033 item 4).
    RunnerAttestation,
}

impl KeyPurpose {
    /// Every purpose.
    pub const ALL: [KeyPurpose; 4] = [
        KeyPurpose::Ingress,
        KeyPurpose::Permit,
        KeyPurpose::AcceptanceRecord,
        KeyPurpose::RunnerAttestation,
    ];

    /// The purpose name used in key ids and key files.
    pub fn as_str(self) -> &'static str {
        match self {
            KeyPurpose::Ingress => "ingress",
            KeyPurpose::Permit => "permit",
            KeyPurpose::AcceptanceRecord => "acceptance-record",
            KeyPurpose::RunnerAttestation => "runner-attestation",
        }
    }

    /// The purpose named `name`, exactly.
    pub fn parse(name: &str) -> Option<KeyPurpose> {
        KeyPurpose::ALL.into_iter().find(|p| p.as_str() == name)
    }
}

impl fmt::Display for KeyPurpose {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A key id `"<purpose>/<label>"`: the purpose name, `/`, then a non-empty
/// label of `[a-z0-9._-]`, at most [`MAX_KEY_ID_BYTES`] bytes in all. Ids are
/// never reused (the registry refuses an id presented with other material).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct KeyId {
    text: String,
    purpose: KeyPurpose,
}

impl KeyId {
    /// The id `text`, or `None` when it is not of the form above.
    pub fn parse(text: &str) -> Option<KeyId> {
        if text.len() > MAX_KEY_ID_BYTES {
            return None;
        }
        let (purpose, label) = text.split_once('/')?;
        let purpose = KeyPurpose::parse(purpose)?;
        let label_ok = !label.is_empty()
            && label
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b));
        label_ok.then(|| KeyId {
            text: text.to_string(),
            purpose,
        })
    }

    /// The id text.
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The purpose the id names.
    pub fn purpose(&self) -> KeyPurpose {
        self.purpose
    }

    /// The key file name: the id with `/` replaced by `_`, suffix `.json`.
    /// Injective: purpose names contain no `_`, so the first `_` is the separator.
    pub fn file_name(&self) -> String {
        format!("{}.json", self.text.replacen('/', "_", 1))
    }

    /// The id whose [`KeyId::file_name`] is `name`, if any.
    pub fn from_file_name(name: &str) -> Option<KeyId> {
        let stem = name.strip_suffix(".json")?;
        let (purpose, label) = stem.split_once('_')?;
        let id = KeyId::parse(&format!("{purpose}/{label}"))?;
        (id.file_name() == name).then_some(id)
    }
}

impl fmt::Display for KeyId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text)
    }
}

/// Where a key stands against a validity window at one trusted instant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Validity {
    /// `not_before <= now < not_after`.
    Valid,
    /// `now < not_before`.
    NotYetValid,
    /// `now >= not_after`.
    Expired,
}

/// A key's validity window in trusted time, whole seconds:
/// `not_before <= now < not_after`, with `not_before < not_after`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyValidity {
    not_before: Timestamp,
    not_after: Timestamp,
}

impl KeyValidity {
    /// The window, or `None` unless both are whole seconds and
    /// `not_before < not_after`.
    pub fn new(not_before: Timestamp, not_after: Timestamp) -> Option<KeyValidity> {
        let whole = not_before.subsec_nanos() == 0 && not_after.subsec_nanos() == 0;
        (whole && not_before < not_after).then_some(KeyValidity {
            not_before,
            not_after,
        })
    }

    /// Start of the window (inclusive).
    pub fn not_before(&self) -> Timestamp {
        self.not_before
    }

    /// End of the window (exclusive).
    pub fn not_after(&self) -> Timestamp {
        self.not_after
    }

    /// Where `now` falls.
    pub fn at(&self, now: Timestamp) -> Validity {
        if now < self.not_before {
            Validity::NotYetValid
        } else if now >= self.not_after {
            Validity::Expired
        } else {
            Validity::Valid
        }
    }
}

/// `sha256:<hex>` of [`MATERIAL_DIGEST_DOMAIN`] followed by the material. It
/// identifies material in the registry and in revocations; it is not secret
/// but is never a substitute for the material.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct MaterialDigest(String);

impl MaterialDigest {
    /// The digest of `material`.
    pub fn of(material: &[u8]) -> MaterialDigest {
        let mut h = Sha256::new();
        h.update(MATERIAL_DIGEST_DOMAIN);
        h.update(material);
        let mut text = String::with_capacity(71);
        text.push_str("sha256:");
        for b in h.finalize() {
            text.push(char::from(b"0123456789abcdef"[usize::from(b >> 4)]));
            text.push(char::from(b"0123456789abcdef"[usize::from(b & 0x0f)]));
        }
        MaterialDigest(text)
    }

    /// The digest from its text form (`sha256:` and 64 lowercase hex digits).
    pub fn parse(text: &str) -> Option<MaterialDigest> {
        let hex = text.strip_prefix("sha256:")?;
        (hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
        .then(|| MaterialDigest(text.to_string()))
    }

    /// The `sha256:<hex>` text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for MaterialDigest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

enum Typed {
    Ingress(IngressKey),
    Permit(PermitKey),
    AcceptanceRecord(AcceptanceRecordKey),
    RunnerAttestation(RunnerAttestationKey),
}

/// Key material tagged with its purpose: it can be used only as the key type
/// of that purpose. Never printed (`Debug` is redacted); the buffer is
/// overwritten on drop (best effort, [`super::overwrite`]).
pub struct Material {
    key: Typed,
    digest: MaterialDigest,
}

impl Material {
    /// Material of `purpose` from raw bytes (at least
    /// [`super::MIN_KEY_BYTES`]); `bytes` is consumed and overwritten on drop.
    pub fn new(purpose: KeyPurpose, bytes: Vec<u8>) -> Result<Material, KeyError> {
        let digest = MaterialDigest::of(&bytes);
        let key = match purpose {
            KeyPurpose::Ingress => Typed::Ingress(IngressKey::from_bytes(bytes)?),
            KeyPurpose::Permit => Typed::Permit(PermitKey::from_bytes(bytes)?),
            KeyPurpose::AcceptanceRecord => {
                Typed::AcceptanceRecord(AcceptanceRecordKey::from_bytes(bytes)?)
            }
            KeyPurpose::RunnerAttestation => {
                Typed::RunnerAttestation(RunnerAttestationKey::from_bytes(bytes)?)
            }
        };
        Ok(Material { key, digest })
    }

    /// The purpose of this material.
    pub fn purpose(&self) -> KeyPurpose {
        match self.key {
            Typed::Ingress(_) => KeyPurpose::Ingress,
            Typed::Permit(_) => KeyPurpose::Permit,
            Typed::AcceptanceRecord(_) => KeyPurpose::AcceptanceRecord,
            Typed::RunnerAttestation(_) => KeyPurpose::RunnerAttestation,
        }
    }

    /// The digest of the material.
    pub fn digest(&self) -> &MaterialDigest {
        &self.digest
    }

    /// The ingress key, when this is ingress material.
    pub fn ingress(&self) -> Option<&IngressKey> {
        match &self.key {
            Typed::Ingress(k) => Some(k),
            _ => None,
        }
    }

    /// The permit key, when this is permit material.
    pub fn permit(&self) -> Option<&PermitKey> {
        match &self.key {
            Typed::Permit(k) => Some(k),
            _ => None,
        }
    }

    /// The acceptance-record key, when this is acceptance-record material.
    pub fn acceptance_record(&self) -> Option<&AcceptanceRecordKey> {
        match &self.key {
            Typed::AcceptanceRecord(k) => Some(k),
            _ => None,
        }
    }

    /// The runner attestation key, when this is runner material.
    pub fn runner_attestation(&self) -> Option<&RunnerAttestationKey> {
        match &self.key {
            Typed::RunnerAttestation(k) => Some(k),
            _ => None,
        }
    }
}

impl fmt::Debug for Material {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Material({}, <redacted>)", self.purpose())
    }
}

/// Whole seconds of `lifetime`, rounded up (saturating).
pub fn whole_seconds_up(lifetime: Duration) -> i64 {
    let secs = i64::try_from(lifetime.as_secs()).unwrap_or(i64::MAX);
    secs.saturating_add(i64::from(lifetime.subsec_nanos() > 0))
}

/// A source of purpose-separated keys with ids and validity windows (J-3).
///
/// `key` answers for one id at trusted time `now`: a key of another purpose
/// than `purpose` is [`KeyStatus::Unknown`]; outside its window it is
/// [`KeyStatus::Expired`] or [`KeyStatus::NotYetValid`]. A source never
/// substitutes for the ledger revocation lookup, which callers perform inside
/// the transaction that consumes the authenticated object ([`key_at_use`]).
pub trait KeySource {
    /// The status of `id` for `purpose` at `now`.
    fn key(
        &self,
        purpose: KeyPurpose,
        id: &KeyId,
        now: Timestamp,
    ) -> Result<KeyStatus<'_, Material>, KeysUnavailable>;

    /// The key with which to issue an object of `purpose` that lives for
    /// `lifetime` from `now`: never revoked, expired or not yet valid; its
    /// `not_after` is at least `now + lifetime + margin`; among those, the
    /// latest `not_before`. None qualifying is [`KeysUnavailable`] (issuing
    /// refuses rather than produce an object that outlives its key).
    fn issuing_key(
        &self,
        purpose: KeyPurpose,
        now: Timestamp,
        lifetime: Duration,
    ) -> Result<(KeyId, &Material), KeysUnavailable>;

    /// The J-3 issuing rule for one configured key `id` (a seal's binding
    /// names its ingress key, J-6): `id` must be of `purpose`, not revoked,
    /// valid at `now`, and its `not_after` at least `now + lifetime + margin`;
    /// otherwise [`KeysUnavailable`] (M5a-13 L9).
    fn issuing_named_key(
        &self,
        purpose: KeyPurpose,
        id: &KeyId,
        now: Timestamp,
        lifetime: Duration,
    ) -> Result<&Material, KeysUnavailable>;
}

/// What the key registry says about a key presented at load.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admission {
    /// First seen (now recorded) or seen before with the same purpose and material.
    Admitted,
    /// The id was registered before with other material or another purpose.
    IdConflict,
    /// The material digest is registered under another id or purpose.
    MaterialConflict,
    /// The id or the material digest was revoked (ever).
    Revoked,
}

/// A recorded key revocation (`key_revocations`, J-3). Revocation applies to
/// the id and to the material under any id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyRevocation {
    /// The revoked id.
    pub key_id: KeyId,
    /// The digest of the material the id named.
    pub material: MaterialDigest,
    /// The purpose of the key.
    pub purpose: KeyPurpose,
    /// Trusted time of the revocation.
    pub revoked_at: Timestamp,
    /// The operator principal that recorded it (`admin.sock`, J-10 Q-3).
    pub actor: String,
    /// Free-text reason (never material).
    pub reason: String,
}

/// The authenticated OS peer that recorded a key revocation (J-11.8 audit
/// provenance). It proves an OS principal only (M5-D7), never a human; the pid
/// is recorded for audit only. M5a-13 L9: every revocation path records this
/// structured actor, never free text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OsActor {
    /// The peer uid from `SO_PEERCRED`.
    pub uid: u32,
    /// The peer pid from `SO_PEERCRED` (audit only).
    pub pid: i32,
}

impl OsActor {
    /// The canonical actor document recorded in `key_revocations.actor`.
    pub fn document(&self) -> Option<String> {
        json::canonical(&json::obj([
            ("principal_kind", json::s("os")),
            ("os_principal", json::s(&format!("uid:{}", self.uid))),
            ("verified_by", json::s("so_peercred")),
            ("endpoint", json::s("admin.sock")),
            ("pid", Value::Int(i64::from(self.pid))),
        ]))
        .ok()
    }

    /// The actor a canonical document records; `None` unless `text` is exactly
    /// [`OsActor::document`] of some actor.
    pub fn from_document(text: &str) -> Option<OsActor> {
        let v = json::parse(text.as_bytes()).ok()?;
        let m = v.as_obj()?;
        let uid = m
            .get("os_principal")?
            .as_str()?
            .strip_prefix("uid:")?
            .parse()
            .ok()?;
        let pid = i32::try_from(m.get("pid")?.as_int()?).ok()?;
        let actor = OsActor { uid, pid };
        (actor.document()?.as_str() == text).then_some(actor)
    }
}

/// The revocation lookup used at use time, inside the ledger transaction that
/// consumes the authenticated object (J-3). No cache.
pub trait KeyRevocations {
    /// `true` when `id` or `material` was ever revoked.
    fn is_revoked(&self, id: &KeyId, material: &MaterialDigest) -> Result<bool, KeysUnavailable>;
}

/// The ledger's key registry (`key_registry`, `key_revocations`): every id
/// ever loaded with its purpose and material digest, and every revocation.
/// Ids are never reused; revoked material is refused under any id.
pub trait KeyRegistry: KeyRevocations {
    /// Admit a key at load: record `(id, purpose, material)` the first time;
    /// refuse a known id with other material or purpose, material already
    /// registered under another id or purpose, and a revoked id or material.
    fn admit(
        &mut self,
        id: &KeyId,
        purpose: KeyPurpose,
        material: &MaterialDigest,
    ) -> Result<Admission, KeysUnavailable>;

    /// Record `revocation` (idempotent for the same id).
    fn revoke(&mut self, revocation: KeyRevocation) -> Result<(), KeysUnavailable>;
}

/// [`KeySource::key`] followed by the revocation lookup in `revocations`
/// (the caller's open ledger transaction): an active key whose id or material
/// was revoked is [`KeyStatus::Revoked`].
pub fn key_at_use<'a>(
    source: &'a dyn KeySource,
    revocations: &dyn KeyRevocations,
    purpose: KeyPurpose,
    id: &KeyId,
    now: Timestamp,
) -> Result<KeyStatus<'a, Material>, KeysUnavailable> {
    match source.key(purpose, id, now)? {
        KeyStatus::Active(m) if revocations.is_revoked(id, m.digest())? => Ok(KeyStatus::Revoked),
        other => Ok(other),
    }
}

/// [`KeySource::issuing_key`] followed by the revocation lookup: a selected
/// key that the ledger has revoked refuses issuing ([`KeysUnavailable`]) until
/// the source is reloaded without it.
pub fn issuing_key_at_use<'a>(
    source: &'a dyn KeySource,
    revocations: &dyn KeyRevocations,
    purpose: KeyPurpose,
    now: Timestamp,
    lifetime: Duration,
) -> Result<(KeyId, &'a Material), KeysUnavailable> {
    let (id, m) = source.issuing_key(purpose, now, lifetime)?;
    if revocations.is_revoked(&id, m.digest())? {
        return Err(KeysUnavailable);
    }
    Ok((id, m))
}

/// [`KeySource::issuing_named_key`] followed by the ledger revocation lookup.
pub fn issuing_named_key_at_use<'a>(
    source: &'a dyn KeySource,
    revocations: &dyn KeyRevocations,
    purpose: KeyPurpose,
    id: &KeyId,
    now: Timestamp,
    lifetime: Duration,
) -> Result<&'a Material, KeysUnavailable> {
    let m = source.issuing_named_key(purpose, id, now, lifetime)?;
    if revocations.is_revoked(id, m.digest())? {
        return Err(KeysUnavailable);
    }
    Ok(m)
}

/// An in-memory [`KeyRegistry`] with the ledger's semantics. Test and harness
/// use; the production registry is the ledger's.
#[derive(Debug, Default)]
pub struct MemoryKeyRegistry {
    ids: BTreeMap<KeyId, (KeyPurpose, MaterialDigest)>,
    digests: BTreeMap<MaterialDigest, (KeyId, KeyPurpose)>,
    revoked_ids: BTreeSet<KeyId>,
    revoked_digests: BTreeSet<MaterialDigest>,
    revocations: Vec<KeyRevocation>,
    unavailable: bool,
}

impl MemoryKeyRegistry {
    /// An empty registry.
    pub fn new() -> MemoryKeyRegistry {
        MemoryKeyRegistry::default()
    }

    /// Make every operation fail as unavailable while `true`.
    pub fn set_unavailable(&mut self, unavailable: bool) {
        self.unavailable = unavailable;
    }

    /// Recorded revocations, in order.
    pub fn revocations(&self) -> &[KeyRevocation] {
        &self.revocations
    }

    fn available(&self) -> Result<(), KeysUnavailable> {
        if self.unavailable {
            Err(KeysUnavailable)
        } else {
            Ok(())
        }
    }
}

impl KeyRevocations for MemoryKeyRegistry {
    fn is_revoked(&self, id: &KeyId, material: &MaterialDigest) -> Result<bool, KeysUnavailable> {
        self.available()?;
        Ok(self.revoked_ids.contains(id) || self.revoked_digests.contains(material))
    }
}

impl KeyRegistry for MemoryKeyRegistry {
    fn admit(
        &mut self,
        id: &KeyId,
        purpose: KeyPurpose,
        material: &MaterialDigest,
    ) -> Result<Admission, KeysUnavailable> {
        if self.is_revoked(id, material)? {
            return Ok(Admission::Revoked);
        }
        if let Some(known) = self.ids.get(id) {
            return Ok(if known == &(purpose, material.clone()) {
                Admission::Admitted
            } else {
                Admission::IdConflict
            });
        }
        if self.digests.contains_key(material) {
            return Ok(Admission::MaterialConflict);
        }
        self.ids.insert(id.clone(), (purpose, material.clone()));
        self.digests.insert(material.clone(), (id.clone(), purpose));
        Ok(Admission::Admitted)
    }

    fn revoke(&mut self, revocation: KeyRevocation) -> Result<(), KeysUnavailable> {
        self.available()?;
        self.revoked_ids.insert(revocation.key_id.clone());
        self.revoked_digests.insert(revocation.material.clone());
        self.revocations.push(revocation);
        Ok(())
    }
}

/// A [`KeySource`] presented as the purpose-typed M3/M4 rings at one trusted
/// instant. Key ids that are not [`KeyId`]s are unknown. The ledger
/// revocation lookup is still the caller's (inside the promotion transaction).
pub struct SourceRing<'a> {
    source: &'a dyn KeySource,
    now: Timestamp,
}

impl<'a> SourceRing<'a> {
    /// The rings of `source` at trusted time `now`.
    pub fn new(source: &'a dyn KeySource, now: Timestamp) -> SourceRing<'a> {
        SourceRing { source, now }
    }

    fn typed<K>(
        &self,
        purpose: KeyPurpose,
        key_id: &str,
        pick: fn(&Material) -> Option<&K>,
    ) -> Result<KeyStatus<'a, K>, KeysUnavailable> {
        let Some(id) = KeyId::parse(key_id) else {
            return Ok(KeyStatus::Unknown);
        };
        Ok(match self.source.key(purpose, &id, self.now)? {
            KeyStatus::Active(m) => pick(m).map_or(KeyStatus::Unknown, KeyStatus::Active),
            KeyStatus::Revoked => KeyStatus::Revoked,
            KeyStatus::Unknown => KeyStatus::Unknown,
            KeyStatus::Expired => KeyStatus::Expired,
            KeyStatus::NotYetValid => KeyStatus::NotYetValid,
        })
    }
}

impl AcceptanceKeyRing for SourceRing<'_> {
    fn record_key(
        &self,
        key_id: &str,
    ) -> Result<KeyStatus<'_, AcceptanceRecordKey>, KeysUnavailable> {
        self.typed(
            KeyPurpose::AcceptanceRecord,
            key_id,
            Material::acceptance_record,
        )
    }
}

impl RunnerKeyRing for SourceRing<'_> {
    fn runner_key(
        &self,
        key_id: &str,
    ) -> Result<KeyStatus<'_, RunnerAttestationKey>, KeysUnavailable> {
        self.typed(
            KeyPurpose::RunnerAttestation,
            key_id,
            Material::runner_attestation,
        )
    }
}

/// A [`KeySource`] filled explicitly in memory, with the selection rules of
/// J-3. Test and harness use (and the reference for implementations).
#[derive(Debug, Default)]
pub struct MemoryKeySource {
    keys: BTreeMap<KeyId, (Material, KeyValidity)>,
    revoked: BTreeSet<KeyId>,
    margin_seconds: i64,
    unavailable: bool,
}

impl MemoryKeySource {
    /// An empty source with issuing margin `margin_seconds`.
    pub fn new(margin_seconds: i64) -> MemoryKeySource {
        MemoryKeySource {
            margin_seconds,
            ..MemoryKeySource::default()
        }
    }

    /// Add `material` as `id` with `validity`; `false` (nothing added) when the
    /// id's purpose differs from the material's or the id is already present.
    pub fn insert(&mut self, id: KeyId, material: Material, validity: KeyValidity) -> bool {
        if id.purpose() != material.purpose() || self.keys.contains_key(&id) {
            return false;
        }
        self.keys.insert(id, (material, validity));
        true
    }

    /// Mark `id` revoked in this source.
    pub fn revoke(&mut self, id: &KeyId) {
        self.revoked.insert(id.clone());
    }

    /// Make every lookup fail as unavailable while `true`.
    pub fn set_unavailable(&mut self, unavailable: bool) {
        self.unavailable = unavailable;
    }
}

/// The J-3 lookup over `(id, material, validity)` entries with `revoked` ids.
pub fn lookup<'a>(
    entry: Option<(&'a Material, &KeyValidity)>,
    revoked: bool,
    purpose: KeyPurpose,
    now: Timestamp,
) -> KeyStatus<'a, Material> {
    let Some((m, validity)) = entry else {
        return KeyStatus::Unknown;
    };
    if m.purpose() != purpose {
        return KeyStatus::Unknown;
    }
    if revoked {
        return KeyStatus::Revoked;
    }
    match validity.at(now) {
        Validity::Valid => KeyStatus::Active(m),
        Validity::Expired => KeyStatus::Expired,
        Validity::NotYetValid => KeyStatus::NotYetValid,
    }
}

/// The J-3 issuing selection over `(id, material, validity, revoked)`
/// candidates: valid at `now`, not revoked, `not_after >= now + lifetime +
/// margin`; the latest `not_before` wins, ties broken by the greater id.
pub fn select_issuing<'a, I>(
    candidates: I,
    purpose: KeyPurpose,
    now: Timestamp,
    lifetime: Duration,
    margin_seconds: i64,
) -> Result<(KeyId, &'a Material), KeysUnavailable>
where
    I: IntoIterator<Item = (&'a KeyId, &'a Material, &'a KeyValidity, bool)>,
{
    let horizon = now
        .plus_seconds(whole_seconds_up(lifetime))
        .plus_seconds(margin_seconds.max(0));
    candidates
        .into_iter()
        .filter(|(id, m, v, revoked)| {
            id.purpose() == purpose
                && m.purpose() == purpose
                && !revoked
                && v.at(now) == Validity::Valid
                && v.not_after() >= horizon
        })
        .max_by(|a, b| (a.2.not_before(), a.0).cmp(&(b.2.not_before(), b.0)))
        .map(|(id, m, _, _)| (id.clone(), m))
        .ok_or(KeysUnavailable)
}

impl KeySource for MemoryKeySource {
    fn key(
        &self,
        purpose: KeyPurpose,
        id: &KeyId,
        now: Timestamp,
    ) -> Result<KeyStatus<'_, Material>, KeysUnavailable> {
        if self.unavailable {
            return Err(KeysUnavailable);
        }
        Ok(lookup(
            self.keys.get(id).map(|(m, v)| (m, v)),
            self.revoked.contains(id),
            purpose,
            now,
        ))
    }

    fn issuing_key(
        &self,
        purpose: KeyPurpose,
        now: Timestamp,
        lifetime: Duration,
    ) -> Result<(KeyId, &Material), KeysUnavailable> {
        if self.unavailable {
            return Err(KeysUnavailable);
        }
        select_issuing(
            self.keys
                .iter()
                .map(|(id, (m, v))| (id, m, v, self.revoked.contains(id))),
            purpose,
            now,
            lifetime,
            self.margin_seconds,
        )
    }

    fn issuing_named_key(
        &self,
        purpose: KeyPurpose,
        id: &KeyId,
        now: Timestamp,
        lifetime: Duration,
    ) -> Result<&Material, KeysUnavailable> {
        if self.unavailable {
            return Err(KeysUnavailable);
        }
        select_issuing(
            self.keys
                .get_key_value(id)
                .map(|(id, (m, v))| (id, m, v, self.revoked.contains(id))),
            purpose,
            now,
            lifetime,
            self.margin_seconds,
        )
        .map(|(_, m)| m)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn t(s: &str) -> Timestamp {
        Timestamp::parse_strict(s).unwrap()
    }

    fn id(s: &str) -> KeyId {
        KeyId::parse(s).unwrap()
    }

    fn window(a: &str, b: &str) -> KeyValidity {
        KeyValidity::new(t(a), t(b)).unwrap()
    }

    fn mat(p: KeyPurpose, b: u8) -> Material {
        Material::new(p, vec![b; 32]).unwrap()
    }

    #[test]
    fn key_ids_are_purpose_prefixed_bounded_and_map_to_file_names() {
        let k = id("permit/2026-11.a-b");
        assert_eq!(k.purpose(), KeyPurpose::Permit);
        assert_eq!(k.file_name(), "permit_2026-11.a-b.json");
        assert_eq!(KeyId::from_file_name(&k.file_name()), Some(k));
        let r = id("runner-attestation/x");
        assert_eq!(r.file_name(), "runner-attestation_x.json");
        assert_eq!(KeyId::from_file_name("runner-attestation_x.json"), Some(r));
        let u = id("acceptance-record/a_b");
        assert_eq!(u.file_name(), "acceptance-record_a_b.json");
        assert_eq!(KeyId::from_file_name("acceptance-record_a_b.json"), Some(u));
        for bad in [
            "",
            "permit",
            "permit/",
            "/x",
            "other/x",
            "Permit/x",
            "permit/X",
            "permit/a/b",
            "permit/a b",
            "permit/é",
        ] {
            assert!(KeyId::parse(bad).is_none(), "{bad}");
        }
        assert!(KeyId::parse(&format!("permit/{}", "a".repeat(57))).is_some());
        assert!(KeyId::parse(&format!("permit/{}", "a".repeat(58))).is_none());
        for bad in [
            "permit_x",
            "permit_x.JSON",
            "permit-x.json",
            ".permit_x.json",
            "permit_x.json.tmp",
        ] {
            assert!(KeyId::from_file_name(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn validity_windows_are_half_open_whole_seconds() {
        let w = window("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z");
        assert_eq!(w.at(t("2026-10-31T23:59:59Z")), Validity::NotYetValid);
        assert_eq!(w.at(t("2026-11-01T00:00:00Z")), Validity::Valid);
        assert_eq!(w.at(t("2026-11-30T23:59:59Z")), Validity::Valid);
        assert_eq!(w.at(t("2026-12-01T00:00:00Z")), Validity::Expired);
        assert!(KeyValidity::new(t("2026-11-01T00:00:00Z"), t("2026-11-01T00:00:00Z")).is_none());
        assert!(KeyValidity::new(
            Timestamp::parse_rfc3339("2026-11-01T00:00:00.5Z").unwrap(),
            t("2026-12-01T00:00:00Z")
        )
        .is_none());
    }

    #[test]
    fn material_is_purpose_tagged_redacted_and_digested_without_purpose() {
        let a = mat(KeyPurpose::Permit, 0xab);
        let b = mat(KeyPurpose::Ingress, 0xab);
        assert_eq!(a.digest(), b.digest(), "digest ignores purpose");
        assert!(a.permit().is_some() && a.ingress().is_none());
        assert!(b.ingress().is_some() && b.permit().is_none());
        let text = format!("{a:?}");
        assert!(text.contains("redacted") && !text.contains("ab") && !text.contains("171"));
        assert!(MaterialDigest::parse(a.digest().as_str()).is_some());
        assert_ne!(
            MaterialDigest::of(&[0xab; 32]).as_str(),
            writ_kernel::json::Digest::of_bytes(&[0xab; 32]).as_str(),
            "domain-separated"
        );
        assert!(Material::new(KeyPurpose::Permit, vec![1; 31]).is_err());
    }

    #[test]
    fn memory_registry_never_reuses_ids_and_refuses_revoked_material() {
        let mut r = MemoryKeyRegistry::new();
        let d1 = MaterialDigest::of(&[1; 32]);
        let d2 = MaterialDigest::of(&[2; 32]);
        let a = id("permit/a");
        assert_eq!(
            r.admit(&a, KeyPurpose::Permit, &d1),
            Ok(Admission::Admitted)
        );
        assert_eq!(
            r.admit(&a, KeyPurpose::Permit, &d1),
            Ok(Admission::Admitted)
        );
        assert_eq!(
            r.admit(&a, KeyPurpose::Permit, &d2),
            Ok(Admission::IdConflict)
        );
        assert_eq!(
            r.admit(&id("permit/b"), KeyPurpose::Permit, &d1),
            Ok(Admission::MaterialConflict)
        );
        assert_eq!(
            r.admit(&id("ingress/b"), KeyPurpose::Ingress, &d1),
            Ok(Admission::MaterialConflict)
        );
        r.revoke(KeyRevocation {
            key_id: a.clone(),
            material: d1.clone(),
            purpose: KeyPurpose::Permit,
            revoked_at: t("2026-11-02T00:00:00Z"),
            actor: "uid:0".into(),
            reason: "test".into(),
        })
        .unwrap();
        assert_eq!(r.admit(&a, KeyPurpose::Permit, &d1), Ok(Admission::Revoked));
        assert_eq!(
            r.admit(&id("permit/c"), KeyPurpose::Permit, &d1),
            Ok(Admission::Revoked),
            "revoked material under a new id"
        );
        assert_eq!(r.is_revoked(&id("permit/z"), &d1), Ok(true));
        assert_eq!(r.is_revoked(&a, &d2), Ok(true));
        assert_eq!(r.is_revoked(&id("permit/z"), &d2), Ok(false));
        r.set_unavailable(true);
        assert_eq!(r.is_revoked(&a, &d2), Err(KeysUnavailable));
    }

    #[test]
    fn lookup_separates_purposes_and_applies_windows_and_revocation() {
        let mut s = MemoryKeySource::new(60);
        let k = id("permit/a");
        assert!(s.insert(
            k.clone(),
            mat(KeyPurpose::Permit, 1),
            window("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z")
        ));
        assert!(!s.insert(
            id("ingress/a"),
            mat(KeyPurpose::Permit, 2),
            window("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z")
        ));
        let now = t("2026-11-15T00:00:00Z");
        assert!(matches!(
            s.key(KeyPurpose::Permit, &k, now),
            Ok(KeyStatus::Active(_))
        ));
        assert!(matches!(
            s.key(KeyPurpose::Ingress, &k, now),
            Ok(KeyStatus::Unknown)
        ));
        assert!(matches!(
            s.key(KeyPurpose::Permit, &k, t("2026-12-01T00:00:00Z")),
            Ok(KeyStatus::Expired)
        ));
        assert!(matches!(
            s.key(KeyPurpose::Permit, &k, t("2026-10-01T00:00:00Z")),
            Ok(KeyStatus::NotYetValid)
        ));
        let mut reg = MemoryKeyRegistry::new();
        assert!(matches!(
            key_at_use(&s, &reg, KeyPurpose::Permit, &k, now),
            Ok(KeyStatus::Active(_))
        ));
        let digest = MaterialDigest::of(&[1; 32]);
        reg.revoke(KeyRevocation {
            key_id: id("permit/other"),
            material: digest,
            purpose: KeyPurpose::Permit,
            revoked_at: now,
            actor: "uid:0".into(),
            reason: "material compromised".into(),
        })
        .unwrap();
        assert!(matches!(
            key_at_use(&s, &reg, KeyPurpose::Permit, &k, now),
            Ok(KeyStatus::Revoked)
        ));
        assert_eq!(
            issuing_key_at_use(&s, &reg, KeyPurpose::Permit, now, Duration::from_secs(30)).err(),
            Some(KeysUnavailable)
        );
        s.revoke(&k);
        assert!(matches!(
            s.key(KeyPurpose::Permit, &k, now),
            Ok(KeyStatus::Revoked)
        ));
        s.set_unavailable(true);
        assert!(s.key(KeyPurpose::Permit, &k, now).is_err());
    }

    #[test]
    fn issuing_prefers_latest_not_before_and_excludes_near_expiry() {
        let mut s = MemoryKeySource::new(60);
        let old = id("permit/old");
        let new = id("permit/new");
        let future = id("permit/future");
        s.insert(
            old.clone(),
            mat(KeyPurpose::Permit, 1),
            window("2026-11-01T00:00:00Z", "2026-11-15T00:02:00Z"),
        );
        s.insert(
            new.clone(),
            mat(KeyPurpose::Permit, 2),
            window("2026-11-14T00:00:00Z", "2026-12-15T00:00:00Z"),
        );
        s.insert(
            future,
            mat(KeyPurpose::Permit, 3),
            window("2026-11-20T00:00:00Z", "2026-12-30T00:00:00Z"),
        );
        let ttl = Duration::from_secs(30);
        let now = t("2026-11-15T00:00:00Z");
        assert_eq!(s.issuing_key(KeyPurpose::Permit, now, ttl).unwrap().0, new);
        s.revoke(&new);
        // old: not_after 00:02:00 >= now + 30 + 60 = 00:01:30
        assert_eq!(s.issuing_key(KeyPurpose::Permit, now, ttl).unwrap().0, old);
        // at 00:00:31 the horizon is 00:02:01: old is near expiry, nothing qualifies.
        assert!(s
            .issuing_key(KeyPurpose::Permit, t("2026-11-15T00:00:31Z"), ttl)
            .is_err());
        // Overlap: old still verifies while it is no longer selected.
        assert!(matches!(
            s.key(KeyPurpose::Permit, &old, t("2026-11-15T00:01:59Z")),
            Ok(KeyStatus::Active(_))
        ));
        assert!(s.issuing_key(KeyPurpose::Ingress, now, ttl).is_err());
        assert_eq!(whole_seconds_up(Duration::from_millis(30_001)), 31);
    }

    #[test]
    fn os_actor_documents_round_trip_and_free_text_is_refused() {
        let a = OsActor { uid: 0, pid: 42 };
        let doc = a.document().unwrap();
        assert_eq!(OsActor::from_document(&doc), Some(a));
        for bad in [
            "uid:0",
            "operator",
            "{}",
            r#"{"os_principal":"uid:0","pid":1}"#,
            &doc.replace("admin.sock", "host.sock"),
            &doc.replace("\"os\"", "\"human\""),
        ] {
            assert_eq!(OsActor::from_document(bad), None, "{bad}");
        }
    }

    #[test]
    fn source_ring_presents_typed_rings() {
        let mut s = MemoryKeySource::new(0);
        s.insert(
            id("acceptance-record/a"),
            mat(KeyPurpose::AcceptanceRecord, 1),
            window("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z"),
        );
        s.insert(
            id("runner-attestation/a"),
            mat(KeyPurpose::RunnerAttestation, 2),
            window("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z"),
        );
        let ring = SourceRing::new(&s, t("2026-11-15T00:00:00Z"));
        assert!(matches!(
            ring.record_key("acceptance-record/a"),
            Ok(KeyStatus::Active(_))
        ));
        assert!(matches!(
            ring.record_key("runner-attestation/a"),
            Ok(KeyStatus::Unknown)
        ));
        assert!(matches!(
            ring.record_key("not an id"),
            Ok(KeyStatus::Unknown)
        ));
        assert!(matches!(
            ring.runner_key("runner-attestation/a"),
            Ok(KeyStatus::Active(_))
        ));
        let late = SourceRing::new(&s, t("2026-12-01T00:00:00Z"));
        assert!(matches!(
            late.runner_key("runner-attestation/a"),
            Ok(KeyStatus::Expired)
        ));
    }
}
