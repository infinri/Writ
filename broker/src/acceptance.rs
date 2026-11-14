//! Authenticated acceptance records, runner attestations and their use-time
//! checks (M3-05; D-036 items 1 and 5, D-033 item 4, D-034; ADR-0008
//! APPROVED IN PRINCIPLE; INV-32..INV-40).
//!
//! **The authenticated record contract** `authenticated-acceptance-record/1`
//! is a closed object:
//!
//! ```text
//! { "contract_version": "authenticated-acceptance-record/1",
//!   "purpose": "writ.acceptance-record.v1",
//!   "issuer": <id>,                      acceptance service identity
//!   "mode": "governed" | "advisory",
//!   "record": <writ-acceptance record>,  the kernel AcceptanceRecord contract
//!   "evaluations": [ { "criterion_id", "evaluation_id", "evaluator_id",
//!       "evaluator_version", "implementation_digest", "environment_digest",
//!       "registry_revision", "attestation_id": <id> | null,
//!       "evidence_digests": [<digest>, ...] }, ... ],
//!   "auth": { "alg": "hmac-sha256", "key_id": <id>, "mac": <64 hex> } }
//! ```
//!
//! **MAC bytes:** the UTF-8 bytes of the D-006 canonical JSON of the whole
//! document with the single member `auth.mac` removed. Everything else is
//! authenticated: contract version, purpose, issuer, mode, the complete
//! record (identity, scope, transition, artifact snapshot and digest,
//! acceptance policy, revision, status, issue and expiry times, every
//! criterion with its evaluator, trust label and freshness), the evaluation
//! bindings (evaluator identity, version, digests, registry revision,
//! attestation reference, evidence digests) and `auth.alg` and `auth.key_id`.
//! The key is an [`AcceptanceRecordKey`] (its own type and purpose); the
//! purpose string inside the authenticated bytes separates the domain from
//! runner attestations (`writ.runner-attestation.v1`), seals and permits.
//!
//! **Validated projection:** after the MAC verifies, the `record` member, and
//! only it, is passed unchanged to the kernel contract
//! ([`writ_kernel::contracts::AcceptanceRecord::from_value`]); the kernel's
//! unkeyed `record_digest` stays a consistency check. The evaluations must
//! name exactly the record's criteria, each with the criterion's evaluator;
//! a criterion with trust `M` must name an attestation, any other must not.
//!
//! **Amended contract** `authenticated-acceptance-record/2` (M4-01; D-040
//! O-4; M4-INTERFACES I-5): identical to `/1` (same purpose, MAC rule and
//! projection) except that every evaluation has one more member,
//! `"signoff_id": <id> | null`, non-null exactly when the criterion's trust
//! is `H` (and `attestation_id` exactly when it is `M`). The contract
//! version is inside the MAC bytes, so a `/1` record can never be presented
//! as `/2`. A `/1` record never names a signoff: its human criteria keep
//! being refused at use time (`ACCEPTANCE_HUMAN_UNAVAILABLE`), never
//! reinterpreted; mechanical-only `/1` records stay valid.
//! [`VerifiedRecord::signoff_ids`] exposes the parsed references, never
//! authority (R8); [`check_use`] decides each one (below).
//!
//! **Verification order** ([`verify_record`]): envelope shape
//! (`ACCEPTANCE_RECORD_MALFORMED`); key lookup (unavailable ring:
//! `ACCEPTANCE_KEY_UNAVAILABLE`; unknown or revoked key:
//! `ACCEPTANCE_RECORD_UNAUTHENTICATED`); MAC
//! (`ACCEPTANCE_RECORD_UNAUTHENTICATED`); projection and evaluation bindings
//! (`ACCEPTANCE_RECORD_MALFORMED`); `mode` advisory
//! (`ACCEPTANCE_ADVISORY`, INV-39). A valid MAC authenticates the issuer
//! only: it never replaces the use-time checks below.
//!
//! **Runner attestations** ([`verify_attestation`]): semantic validation of
//! `runner-attestation/1` ([`crate::contracts::RunnerAttestation`]), key
//! lookup in the [`RunnerKeyRing`], MAC over the canonical document without
//! `auth.mac`.
//!
//! **Use-time checks** ([`check_use`], run inside the promotion transaction
//! on trusted stored state): the kernel acceptance check
//! ([`writ_kernel::acceptance::check`]: scope, transition, artifact, policy,
//! revision, expiry, criteria, trust labels, revoked evaluators, freshness,
//! replay of the record); a `/1` record with a required human criterion is
//! refused (`ACCEPTANCE_HUMAN_UNAVAILABLE`, never reinterpreted); every
//! required criterion's evaluator must have an
//! active registry entry whose scope contains the tenant and project and
//! whose class, trust, version, digests, registry revision and criteria
//! match the binding; a mechanical criterion needs its verified attestation,
//! bound to exactly this record, criterion, transition, artifact, policy,
//! revision, evaluator and registered command, signed with the registry
//! entry's key id, exit status 0, fresh at trusted time, never consumed
//! before. Evidence-class criteria rely on the authenticated record and the
//! registry; resolving their evidence digests against a catalog is M7.
//!
//! **Signoffs** (M4-06; D-040 O-4, O-5, O-7; M4-INTERFACES I-5, X7): for a
//! `/2` record, each required human criterion's evaluation names a signoff
//! id; [`check_use`] verifies it against the ledger state read inside the
//! promotion transaction ([`SignoffUse`]), in this order, each with its I-6
//! code: the signoff exists in the record's tenant and project
//! (`SIGNOFF_UNKNOWN`); its provenance is intact: provenance version 2 and
//! the consumed `signoff` challenge that produced it binds its subject,
//! credential, target, scope, policy, revision and complete displayed context
//! (`SIGNOFF_MISMATCH`); its binding equals the record (record id, criterion,
//! task, workflow, transition, snapshot, artifact digest, acceptance policy
//! id, version and digest, state revision), the active requirement set's
//! policy and the current state (`SIGNOFF_MISMATCH`); neither the signoff nor
//! its credential, signer subject or `SignoffAuthority` role for the scope is
//! revoked (`SIGNOFF_REVOKED`; any committed revocation counts, fail closed);
//! trusted time is before its expiry (`SIGNOFF_EXPIRED`); the signer is a
//! human currently holding `SignoffAuthority` for the scope
//! (`SERVICE_NOT_PERMITTED`, `PERMISSION_MISSING`); separation
//! (`SEPARATION_VIOLATED`) against the exclusions stored with the signoff,
//! the exclusions re-read at use time and this promotion's requester and
//! agent; never consumed ledger-wide nor named twice by this record
//! (`SIGNOFF_CONSUMED`). The signoff ids are consumed atomically with the
//! promotion. Separation covers requester, agent and linked identities only:
//! delegation chains are not stored (G4).
//!
//! Production key provisioning, the acceptance service and the runner are
//! future obligations; the in-memory key rings and test sources do not
//! complete them.

use std::collections::BTreeSet;
use std::fmt;

use writ_kernel::acceptance::{check, AcceptanceRefusal};
use writ_kernel::contracts::{
    AcceptanceRecord, AcceptanceRequirements, CriterionClass, CurrentState, EvaluatorTrust,
};
use writ_kernel::ids::{is_digest, is_id, is_name};
use writ_kernel::json::{self, Map, Value};
use writ_kernel::time::Timestamp;

use crate::approval::{ApprovalCode, Signoff, SignoffExclusions, PROVENANCE_VERSION_2};
use crate::contracts::{RegistryEntry, RunnerAttestation};
use crate::human::{
    check_human_permission, signoff_separation, EnrollmentRecord, Permission, RoleAssignment,
    SubjectId, SubjectRecord, SubjectStatus,
};
use crate::keys::{AcceptanceKeyRing, KeyStatus, RunnerKeyRing};
use crate::mac;

/// `contract_version` of an authenticated acceptance record.
pub const RECORD_CONTRACT: &str = "authenticated-acceptance-record/1";
/// `contract_version` of the amended record with signoff references (M4).
pub const RECORD_CONTRACT_V2: &str = "authenticated-acceptance-record/2";
/// `purpose` of an authenticated acceptance record (domain separation; the
/// same for `/1` and `/2`, whose contract version is authenticated).
pub const RECORD_PURPOSE: &str = "writ.acceptance-record.v1";
/// `purpose` of a runner attestation.
pub const ATTESTATION_PURPOSE: &str = "writ.runner-attestation.v1";
/// The only MAC algorithm.
pub const MAC_ALG: &str = "hmac-sha256";

const ENVELOPE: [&str; 7] = [
    "auth",
    "contract_version",
    "evaluations",
    "issuer",
    "mode",
    "purpose",
    "record",
];
const EVALUATION: [&str; 9] = [
    "attestation_id",
    "criterion_id",
    "environment_digest",
    "evaluation_id",
    "evaluator_id",
    "evaluator_version",
    "evidence_digests",
    "implementation_digest",
    "registry_revision",
];
const EVALUATION_V2: [&str; 10] = [
    "attestation_id",
    "criterion_id",
    "environment_digest",
    "evaluation_id",
    "evaluator_id",
    "evaluator_version",
    "evidence_digests",
    "implementation_digest",
    "registry_revision",
    "signoff_id",
];
const AUTH: [&str; 3] = ["alg", "key_id", "mac"];

/// Internal refusal codes of the acceptance path (agents receive the
/// kernel's generic external code).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AcceptanceCode {
    /// The protected transition names no record id, or the source has none.
    RecordUnknown,
    /// The record source could not be consulted.
    SourceUnavailable,
    /// Envelope, projection or evaluation bindings malformed.
    RecordMalformed,
    /// Unknown or revoked key, wrong algorithm, or MAC mismatch.
    RecordUnauthenticated,
    /// The acceptance-record key ring could not be consulted.
    KeyUnavailable,
    /// `mode` is `advisory`: never satisfies a protected transition (INV-39).
    Advisory,
    /// The request's acceptance attributes differ from the record.
    BindingMismatch,
    /// A kernel acceptance-check refusal.
    Kernel(AcceptanceRefusal),
    /// A required human criterion: unavailable until authenticated signoff (M4).
    HumanUnavailable,
    /// The evaluator has no registry entry.
    EvaluatorUnregistered,
    /// The registry entry is revoked.
    EvaluatorRevoked,
    /// Registry entry and evaluation binding differ (scope, class, trust,
    /// version, digests, registry revision or criteria).
    EvaluatorBindingMismatch,
    /// The registry could not be consulted.
    RegistryUnavailable,
    /// A mechanical criterion's attestation is not available.
    AttestationUnknown,
    /// The attestation source could not be consulted.
    AttestationSourceUnavailable,
    /// Attestation malformed (semantic validation).
    AttestationMalformed,
    /// Unknown or revoked runner key, or MAC mismatch.
    AttestationUnauthenticated,
    /// The runner key ring could not be consulted.
    AttestationKeyUnavailable,
    /// The attestation is bound to another record, criterion, context,
    /// evaluator, command or key.
    AttestationBindingMismatch,
    /// The attested command did not exit with status 0.
    AttestationFailed,
    /// Trusted time is at or after the attestation's `fresh_until`.
    AttestationStale,
    /// The attestation was already consumed (ledger-wide).
    AttestationReplayed,
    /// The immutable snapshot store could not confirm the artifact (INV-40).
    ArtifactIntegrityUnavailable,
    /// The immutable snapshot store holds other content for the snapshot id.
    ArtifactIntegrityViolation,
    /// A signoff use-time refusal (I-5): its I-6 code.
    Signoff(ApprovalCode),
}

impl AcceptanceCode {
    /// Internal code.
    pub fn code(self) -> &'static str {
        use AcceptanceCode::*;
        match self {
            RecordUnknown => "ACCEPTANCE_RECORD_UNKNOWN",
            SourceUnavailable => "ACCEPTANCE_SOURCE_UNAVAILABLE",
            RecordMalformed => "ACCEPTANCE_RECORD_MALFORMED",
            RecordUnauthenticated => "ACCEPTANCE_RECORD_UNAUTHENTICATED",
            KeyUnavailable => "ACCEPTANCE_KEY_UNAVAILABLE",
            Advisory => "ACCEPTANCE_ADVISORY",
            BindingMismatch => "PERMIT_BINDING_MISMATCH",
            Kernel(r) => r.code(),
            HumanUnavailable => "ACCEPTANCE_HUMAN_UNAVAILABLE",
            EvaluatorUnregistered => "EVALUATOR_UNREGISTERED",
            EvaluatorRevoked => "ACCEPTANCE_EVALUATOR_REVOKED",
            EvaluatorBindingMismatch => "EVALUATOR_BINDING_MISMATCH",
            RegistryUnavailable => "EVALUATOR_REGISTRY_UNAVAILABLE",
            AttestationUnknown => "ATTESTATION_UNKNOWN",
            AttestationSourceUnavailable => "ATTESTATION_SOURCE_UNAVAILABLE",
            AttestationMalformed => "ATTESTATION_MALFORMED",
            AttestationUnauthenticated => "ATTESTATION_UNAUTHENTICATED",
            AttestationKeyUnavailable => "ATTESTATION_KEY_UNAVAILABLE",
            AttestationBindingMismatch => "ATTESTATION_BINDING_MISMATCH",
            AttestationFailed => "ATTESTATION_FAILED",
            AttestationStale => "ATTESTATION_STALE",
            AttestationReplayed => "ATTESTATION_REPLAYED",
            ArtifactIntegrityUnavailable => "ARTIFACT_INTEGRITY_UNAVAILABLE",
            ArtifactIntegrityViolation => "ARTIFACT_INTEGRITY_VIOLATION",
            Signoff(c) => c.code(),
        }
    }
}

impl fmt::Display for AcceptanceCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

/// One evaluation binding of an authenticated record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvaluationBinding {
    /// Criterion.
    pub criterion_id: String,
    /// Evaluation id.
    pub evaluation_id: String,
    /// Evaluator id (equals the record criterion's).
    pub evaluator_id: String,
    /// Evaluator version.
    pub evaluator_version: String,
    /// Implementation digest.
    pub implementation_digest: String,
    /// Environment digest.
    pub environment_digest: String,
    /// Registry revision the evaluator was registered at.
    pub registry_revision: i64,
    /// The runner attestation (trust `M` only).
    pub attestation_id: Option<String>,
    /// Evidence digests (bound, not resolved: M7).
    pub evidence_digests: Vec<String>,
    /// The human signoff (`/2` records, trust `H` only; always `None` for `/1`).
    pub signoff_id: Option<String>,
}

/// The verified record contract version.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecordVersion {
    /// `authenticated-acceptance-record/1`: no signoff references.
    V1,
    /// `authenticated-acceptance-record/2`: `signoff_id` exactly for trust `H`.
    V2,
}

/// A record whose envelope, MAC, projection and bindings verified. Only
/// [`verify_record`] builds one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedRecord {
    record: AcceptanceRecord,
    evaluations: Vec<EvaluationBinding>,
    version: RecordVersion,
    issuer: String,
    key_id: String,
}

impl VerifiedRecord {
    /// The kernel projection.
    pub fn record(&self) -> &AcceptanceRecord {
        &self.record
    }

    /// The evaluation bindings, one per record criterion.
    pub fn evaluations(&self) -> &[EvaluationBinding] {
        &self.evaluations
    }

    /// The verified contract version.
    pub fn version(&self) -> RecordVersion {
        self.version
    }

    /// Signoff ids the record names (its trust `H` criteria; empty for `/1`).
    ///
    /// A reference only, never authority (R8): a named signoff proves
    /// nothing until the I-5 use-time checks of [`check_use`] inside the
    /// promotion transaction find it in scope, intact, bound to this record,
    /// its requirements and current state, unrevoked, fresh, signed by a
    /// separated subject holding `SignoffAuthority`, and unconsumed.
    pub fn signoff_ids(&self) -> Vec<&str> {
        self.evaluations
            .iter()
            .filter_map(|e| e.signoff_id.as_deref())
            .collect()
    }

    /// The authenticated issuer.
    pub fn issuer(&self) -> &str {
        &self.issuer
    }

    /// The key id that authenticated it.
    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    /// Attestation ids the record requires (its mechanical criteria).
    pub fn attestation_ids(&self) -> Vec<&str> {
        self.evaluations
            .iter()
            .filter_map(|e| e.attestation_id.as_deref())
            .collect()
    }
}

/// The canonical MAC input of `doc`: D-006 canonical JSON without `auth.mac`.
pub fn mac_bytes(doc: &Value) -> Option<String> {
    let Value::Obj(top) = doc else {
        return None;
    };
    let mut top = top.clone();
    let auth = top.get("auth")?.without("mac");
    top.insert("auth".into(), auth);
    json::canonical(&Value::Obj(top)).ok()
}

fn closed<'v>(v: &'v Value, keys: &[&str]) -> Option<&'v Map> {
    let m = v.as_obj()?;
    (m.len() == keys.len() && keys.iter().all(|k| m.contains_key(*k))).then_some(m)
}

fn text<'v>(m: &'v Map, key: &str, ok: fn(&str) -> bool) -> Option<&'v str> {
    m.get(key).and_then(Value::as_str).filter(|s| ok(s))
}

fn is_hex_tag(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

fn is_version(s: &str) -> bool {
    !s.is_empty() && s.len() <= 64 && s.bytes().all(|b| b.is_ascii_graphic())
}

/// Verify an authenticated acceptance record (module documentation for the
/// order). `keys` is consulted at every call; nothing is cached.
pub fn verify_record(
    doc: &Value,
    keys: &dyn AcceptanceKeyRing,
) -> Result<VerifiedRecord, AcceptanceCode> {
    use AcceptanceCode::*;
    let m = closed(doc, &ENVELOPE).ok_or(RecordMalformed)?;
    let constant = |k: &str, want: &str| m.get(k).and_then(Value::as_str) == Some(want);
    let version = match m.get("contract_version").and_then(Value::as_str) {
        Some(RECORD_CONTRACT) => RecordVersion::V1,
        Some(RECORD_CONTRACT_V2) => RecordVersion::V2,
        _ => return Err(RecordMalformed),
    };
    if !constant("purpose", RECORD_PURPOSE) {
        return Err(RecordMalformed);
    }
    let issuer = text(m, "issuer", is_id).ok_or(RecordMalformed)?;
    let mode = m.get("mode").and_then(Value::as_str);
    if !matches!(mode, Some("governed" | "advisory")) {
        return Err(RecordMalformed);
    }
    let auth = m
        .get("auth")
        .and_then(|a| closed(a, &AUTH))
        .ok_or(RecordMalformed)?;
    let key_id = text(auth, "key_id", is_id).ok_or(RecordMalformed)?;
    let tag = text(auth, "mac", is_hex_tag).ok_or(RecordMalformed)?;
    if text(auth, "alg", |a| a == MAC_ALG).is_none() {
        return Err(RecordUnauthenticated);
    }
    let key = match keys.record_key(key_id).map_err(|_| KeyUnavailable)? {
        KeyStatus::Active(key) => key,
        KeyStatus::Revoked | KeyStatus::Unknown => return Err(RecordUnauthenticated),
    };
    let bytes = mac_bytes(doc).ok_or(RecordMalformed)?;
    if !mac::verify_hex(key.bytes(), bytes.as_bytes(), tag) {
        return Err(RecordUnauthenticated);
    }
    let record = m
        .get("record")
        .and_then(|r| AcceptanceRecord::from_value(r).ok())
        .ok_or(RecordMalformed)?;
    let evaluations = evaluations(m.get("evaluations"), &record, version).ok_or(RecordMalformed)?;
    if mode == Some("advisory") {
        return Err(Advisory);
    }
    Ok(VerifiedRecord {
        record,
        evaluations,
        version,
        issuer: issuer.to_string(),
        key_id: key_id.to_string(),
    })
}

/// The evaluation bindings: exactly one per record criterion, same evaluator,
/// an attestation exactly for trust `M`; for `/2` a signoff exactly for trust `H`.
fn evaluations(
    v: Option<&Value>,
    record: &AcceptanceRecord,
    version: RecordVersion,
) -> Option<Vec<EvaluationBinding>> {
    let items = v?.as_arr()?;
    if items.len() != record.criteria.len() {
        return None;
    }
    let mut seen = BTreeSet::new();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let e = match version {
            RecordVersion::V1 => closed(item, &EVALUATION)?,
            RecordVersion::V2 => closed(item, &EVALUATION_V2)?,
        };
        let criterion_id = text(e, "criterion_id", is_name)?;
        let criterion = record
            .criteria
            .iter()
            .find(|c| c.criterion_id == criterion_id)?;
        if !seen.insert(criterion_id.to_string()) {
            return None;
        }
        let evaluator_id = text(e, "evaluator_id", is_name)?;
        if evaluator_id != criterion.evaluator_id {
            return None;
        }
        let attestation_id = match e.get("attestation_id") {
            Some(Value::Null) => None,
            Some(Value::Str(s)) if is_id(s) => Some(s.clone()),
            _ => return None,
        };
        if attestation_id.is_some() != (criterion.evaluator_trust == EvaluatorTrust::Mechanical) {
            return None;
        }
        let signoff_id = match (version, e.get("signoff_id")) {
            (RecordVersion::V1, None) | (RecordVersion::V2, Some(Value::Null)) => None,
            (RecordVersion::V2, Some(Value::Str(s))) if is_id(s) => Some(s.clone()),
            _ => return None,
        };
        if version == RecordVersion::V2
            && signoff_id.is_some() != (criterion.evaluator_trust == EvaluatorTrust::Human)
        {
            return None;
        }
        let evidence_digests = e
            .get("evidence_digests")?
            .as_arr()?
            .iter()
            .map(|d| d.as_str().filter(|s| is_digest(s)).map(str::to_string))
            .collect::<Option<Vec<_>>>()?;
        let registry_revision = e.get("registry_revision")?.as_int().filter(|r| *r >= 0)?;
        out.push(EvaluationBinding {
            criterion_id: criterion_id.to_string(),
            evaluation_id: text(e, "evaluation_id", is_id)?.to_string(),
            evaluator_id: evaluator_id.to_string(),
            evaluator_version: text(e, "evaluator_version", is_version)?.to_string(),
            implementation_digest: text(e, "implementation_digest", is_digest)?.to_string(),
            environment_digest: text(e, "environment_digest", is_digest)?.to_string(),
            registry_revision,
            attestation_id,
            evidence_digests,
            signoff_id,
        });
    }
    Some(out)
}

/// A runner attestation whose contract and MAC verified. Only
/// [`verify_attestation`] builds one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedAttestation(Value);

impl VerifiedAttestation {
    /// The verified document.
    pub fn as_value(&self) -> &Value {
        &self.0
    }

    /// Its `attestation_id`.
    pub fn attestation_id(&self) -> &str {
        self.0
            .get("attestation_id")
            .and_then(Value::as_str)
            .unwrap_or("")
    }

    fn str(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(Value::as_str)
    }
}

/// Verify a runner attestation: semantic validation at trusted time `now`,
/// key lookup, MAC over the canonical document without `auth.mac`.
pub fn verify_attestation(
    doc: &Value,
    keys: &dyn RunnerKeyRing,
    now: Timestamp,
) -> Result<VerifiedAttestation, AcceptanceCode> {
    use AcceptanceCode::*;
    RunnerAttestation::validate(doc, now).map_err(|_| AttestationMalformed)?;
    let auth = doc
        .get("auth")
        .and_then(Value::as_obj)
        .ok_or(AttestationMalformed)?;
    let key_id = auth
        .get("key_id")
        .and_then(Value::as_str)
        .ok_or(AttestationMalformed)?;
    let tag = auth
        .get("mac")
        .and_then(Value::as_str)
        .ok_or(AttestationMalformed)?;
    if auth.get("alg").and_then(Value::as_str) != Some(MAC_ALG) {
        return Err(AttestationUnauthenticated);
    }
    let key = match keys
        .runner_key(key_id)
        .map_err(|_| AttestationKeyUnavailable)?
    {
        KeyStatus::Active(key) => key,
        KeyStatus::Revoked | KeyStatus::Unknown => return Err(AttestationUnauthenticated),
    };
    let bytes = mac_bytes(doc).ok_or(AttestationMalformed)?;
    if !mac::verify_hex(key.bytes(), bytes.as_bytes(), tag) {
        return Err(AttestationUnauthenticated);
    }
    Ok(VerifiedAttestation(doc.clone()))
}

/// Trusted use-time inputs of [`check_use`], read inside the promotion
/// transaction (registry, revocations and consumption are ledger state).
pub struct UseContext<'a> {
    /// The workflow's current trusted state, the requested transition and
    /// trusted time, with consumed record ids and revoked evaluators.
    pub current: &'a CurrentState,
    /// The active acceptance policy's trusted requirement set.
    pub requirements: &'a AcceptanceRequirements,
    /// The current registry entry of each evaluator the record names.
    pub registry: &'a dyn Fn(&str) -> Option<RegistryEntry>,
    /// The verified attestations the record requires, by id.
    pub attestations: &'a [VerifiedAttestation],
    /// Attestation ids consumed ledger-wide.
    pub consumed_attestations: &'a BTreeSet<String>,
    /// The stored state of every signoff the record names that exists in
    /// the ledger, read inside the promotion transaction (C2).
    pub signoffs: &'a [SignoffUse],
    /// The promotion's own requester and agent (excluded signers, O-7).
    pub promotion_parties: &'a [SubjectId],
}

/// A stored signoff with the ledger state its use-time checks need (I-5),
/// read inside the promotion transaction. A reference only (R8): nothing in
/// it is authority until [`check_use`] decides.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignoffUse {
    /// The signoff (revalidated by the ledger's `from_stored`).
    pub signoff: Signoff,
    /// The consumed `signoff` challenge that produced it binds its subject,
    /// credential, target, scope, policy, revision and context fingerprint.
    pub provenance_intact: bool,
    /// A committed revocation targets the signoff.
    pub signoff_revoked: bool,
    /// The signing credential is revoked.
    pub credential_revoked: bool,
    /// The signer's subject row (`None`: missing, provenance broken).
    pub signer: Option<SubjectRecord>,
    /// The signer's enrollment record.
    pub enrollment: Option<EnrollmentRecord>,
    /// The signer's role assignments.
    pub assignments: Vec<RoleAssignment>,
    /// The exclusions re-read from the ledger now (X7).
    pub exclusions: SignoffExclusions,
    /// Consumed ledger-wide.
    pub consumed: bool,
}

/// What a successful [`check_use`] consumes atomically with the promotion.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Consumption {
    /// Attestation ids (mechanical criteria).
    pub attestation_ids: Vec<String>,
    /// Signoff ids (human criteria of a `/2` record).
    pub signoff_ids: Vec<String>,
}

fn entry_str<'e>(entry: &'e RegistryEntry, path: &[&str]) -> Option<&'e str> {
    let mut v = entry.as_value();
    for k in path {
        v = v.get(k)?;
    }
    v.as_str()
}

fn trust_label(t: EvaluatorTrust) -> &'static str {
    match t {
        EvaluatorTrust::Untrusted => "U",
        EvaluatorTrust::ModelAssisted => "MA",
        EvaluatorTrust::Mechanical => "M",
        EvaluatorTrust::Evidence => "E",
        EvaluatorTrust::Human => "H",
    }
}

fn class_label(c: CriterionClass) -> &'static str {
    match c {
        CriterionClass::Mechanical => "mechanical",
        CriterionClass::Evidence => "evidence",
        CriterionClass::Human => "human",
        CriterionClass::ModelAssisted => "model_assisted",
    }
}

/// The use-time checks (module documentation), in order: kernel check, a
/// `/1` record with a required human criterion, then per required criterion
/// the registry binding, the attestation of a mechanical criterion and the
/// signoff of a human one. Returns what to consume atomically with the
/// promotion.
pub fn check_use(
    verified: &VerifiedRecord,
    ctx: &UseContext<'_>,
) -> Result<Consumption, AcceptanceCode> {
    use AcceptanceCode::*;
    let record = &verified.record;
    check(record, ctx.current, ctx.requirements).map_err(Kernel)?;
    if verified.version == RecordVersion::V1
        && ctx
            .requirements
            .criteria
            .iter()
            .any(|c| c.required_class == CriterionClass::Human)
    {
        return Err(HumanUnavailable);
    }
    let now = ctx.current.now;
    let mut consume = Vec::new();
    let mut signoffs: Vec<String> = Vec::new();
    for required in &ctx.requirements.criteria {
        let Some(criterion) = record
            .criteria
            .iter()
            .find(|c| c.criterion_id == required.criterion_id)
        else {
            return Err(Kernel(AcceptanceRefusal::CriteriaMissing));
        };
        let Some(binding) = verified
            .evaluations
            .iter()
            .find(|e| e.criterion_id == required.criterion_id)
        else {
            return Err(RecordMalformed);
        };
        let entry = (ctx.registry)(&binding.evaluator_id).ok_or(EvaluatorUnregistered)?;
        if entry_str(&entry, &["status"]) != Some("active") {
            return Err(EvaluatorRevoked);
        }
        let revision = entry
            .as_value()
            .get("registry_revision")
            .and_then(Value::as_int);
        let matches = entry_str(&entry, &["evaluator_id"]) == Some(&binding.evaluator_id)
            && entry_str(&entry, &["scope", "tenant_id"]) == Some(&record.tenant_id)
            && entry_str(&entry, &["scope", "project_id"]) == Some(&record.project_id)
            && entry_str(&entry, &["class"]) == Some(class_label(required.required_class))
            && entry_str(&entry, &["trust"]) == Some(trust_label(criterion.evaluator_trust))
            && entry_str(&entry, &["evaluator_version"]) == Some(&binding.evaluator_version)
            && entry_str(&entry, &["implementation_digest"])
                == Some(&binding.implementation_digest)
            && entry_str(&entry, &["environment_digest"]) == Some(&binding.environment_digest)
            && revision == Some(binding.registry_revision)
            && entry
                .as_value()
                .get("criteria")
                .and_then(Value::as_arr)
                .is_some_and(|c| c.iter().any(|x| x.as_str() == Some(&required.criterion_id)));
        if !matches {
            return Err(EvaluatorBindingMismatch);
        }
        if required.required_class == CriterionClass::Human {
            let id = binding.signoff_id.as_deref().ok_or(HumanUnavailable)?;
            check_signoff(id, verified, &required.criterion_id, ctx, &signoffs).map_err(Signoff)?;
            signoffs.push(id.to_string());
            continue;
        }
        if required.required_class != CriterionClass::Mechanical {
            continue;
        }
        let id = binding.attestation_id.as_deref().ok_or(RecordMalformed)?;
        let att = ctx
            .attestations
            .iter()
            .find(|a| a.attestation_id() == id)
            .ok_or(AttestationUnknown)?;
        let target = format!(
            "workflow:{}/{}/{}",
            record.tenant_id, record.project_id, record.workflow_id
        );
        let transition = att.as_value().get("transition");
        let s = |k: &str| att.str(k);
        let bound = s("tenant_id") == Some(&record.tenant_id)
            && s("project_id") == Some(&record.project_id)
            && s("task_id") == Some(&record.task_id)
            && s("workflow_id") == Some(&record.workflow_id)
            && transition
                .and_then(|t| t.get("from_state"))
                .and_then(Value::as_str)
                == Some(&record.transition.from_state)
            && transition
                .and_then(|t| t.get("to_state"))
                .and_then(Value::as_str)
                == Some(&record.transition.to_state)
            && s("artifact_snapshot_id") == Some(&record.artifact_snapshot_id)
            && s("artifact_digest") == Some(&record.artifact_digest)
            && s("canonical_target") == Some(&target)
            && s("acceptance_policy_id") == Some(&record.policy.acceptance_policy_id)
            && s("acceptance_policy_version") == Some(&record.policy.acceptance_policy_version)
            && s("acceptance_policy_bundle_sha")
                == Some(&record.policy.acceptance_policy_bundle_sha)
            && att.as_value().get("state_revision").and_then(Value::as_int)
                == Some(record.state_revision.get())
            && s("acceptance_record_id") == Some(&record.acceptance_record_id)
            && s("criterion_id") == Some(&required.criterion_id)
            && s("evaluator_id") == Some(&binding.evaluator_id)
            && s("evaluator_version") == Some(&binding.evaluator_version)
            && s("implementation_digest") == Some(&binding.implementation_digest)
            && s("environment_digest") == Some(&binding.environment_digest)
            && att
                .as_value()
                .get("registry_revision")
                .and_then(Value::as_int)
                == Some(binding.registry_revision)
            && s("command_id") == entry_str(&entry, &["command", "command_id"])
            && s("command_digest") == entry_str(&entry, &["command", "command_digest"])
            && att
                .as_value()
                .get("auth")
                .and_then(|a| a.get("key_id"))
                .and_then(Value::as_str)
                == entry_str(&entry, &["attestation", "key_id"]);
        if !bound {
            return Err(AttestationBindingMismatch);
        }
        if att.as_value().get("exit_status").and_then(Value::as_int) != Some(0) {
            return Err(AttestationFailed);
        }
        let fresh = s("fresh_until").and_then(Timestamp::parse_strict);
        if !fresh.is_some_and(|f| now < f) {
            return Err(AttestationStale);
        }
        if ctx.consumed_attestations.contains(id) || consume.iter().any(|c| c == id) {
            return Err(AttestationReplayed);
        }
        consume.push(id.to_string());
    }
    Ok(Consumption {
        attestation_ids: consume,
        signoff_ids: signoffs,
    })
}

/// The I-5 use-time checks of one signoff (module documentation, in order).
fn check_signoff(
    id: &str,
    verified: &VerifiedRecord,
    criterion_id: &str,
    ctx: &UseContext<'_>,
    already: &[String],
) -> Result<(), ApprovalCode> {
    use ApprovalCode::*;
    let record = &verified.record;
    let current = ctx.current;
    let now = current.now;
    let u = ctx
        .signoffs
        .iter()
        .find(|u| u.signoff.fields().binding.signoff_id == id)
        .ok_or(SignoffUnknown)?;
    let f = u.signoff.fields();
    let b = &f.binding;
    if b.tenant_id != record.tenant_id || b.project_id != record.project_id {
        return Err(SignoffUnknown);
    }
    if !u.provenance_intact || f.provenance_version != PROVENANCE_VERSION_2 {
        return Err(SignoffMismatch);
    }
    let policy = &ctx.requirements.policy;
    let bound = b.acceptance_record_id == record.acceptance_record_id
        && b.criterion_id == criterion_id
        && b.task_id == record.task_id
        && b.workflow_id == record.workflow_id
        && b.from_state == record.transition.from_state
        && b.to_state == record.transition.to_state
        && b.artifact_snapshot_id == record.artifact_snapshot_id
        && b.artifact_digest == record.artifact_digest
        && b.acceptance_policy_id == record.policy.acceptance_policy_id
        && b.acceptance_policy_version == record.policy.acceptance_policy_version
        && b.acceptance_policy_bundle_sha == record.policy.acceptance_policy_bundle_sha
        && b.state_revision == record.state_revision.get()
        && b.acceptance_policy_id == policy.acceptance_policy_id
        && b.acceptance_policy_version == policy.acceptance_policy_version
        && b.acceptance_policy_bundle_sha == policy.acceptance_policy_bundle_sha
        && b.tenant_id == current.tenant_id
        && b.project_id == current.project_id
        && b.task_id == current.task_id
        && b.workflow_id == current.workflow_id
        && b.from_state == current.transition.from_state
        && b.to_state == current.transition.to_state
        && b.artifact_snapshot_id == current.artifact_snapshot_id
        && b.artifact_digest == current.artifact_digest
        && b.state_revision == current.state_revision.get();
    if !bound {
        return Err(SignoffMismatch);
    }
    let signer = u.signer.as_ref().ok_or(SignoffMismatch)?;
    if signer.subject != f.subject {
        return Err(SignoffMismatch);
    }
    let in_scope = |a: &&RoleAssignment| {
        a.subject == f.subject
            && a.permission == Permission::SignoffAuthority
            && a.tenant_id == b.tenant_id
            && a.project_id == b.project_id
    };
    let role_revoked = u
        .assignments
        .iter()
        .filter(in_scope)
        .any(|a| a.revoked_at.is_some())
        && !u
            .assignments
            .iter()
            .filter(in_scope)
            .any(|a| a.revoked_at.is_none());
    if u.signoff_revoked
        || u.credential_revoked
        || matches!(signer.status, SubjectStatus::Revoked { .. })
        || role_revoked
    {
        return Err(SignoffRevoked);
    }
    if now >= b.expires_at {
        return Err(SignoffExpired);
    }
    check_human_permission(
        signer,
        &u.assignments,
        Permission::SignoffAuthority,
        &b.tenant_id,
        &b.project_id,
        now,
    )?;
    let excluded = f.exclusions.union(&u.exclusions);
    let mut all = excluded.all();
    all.extend(ctx.promotion_parties.iter().cloned());
    signoff_separation(&f.subject, u.enrollment.as_ref(), &all)?;
    if u.consumed || already.iter().any(|a| a == id) {
        return Err(SignoffConsumed);
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod signoff_tests;
#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod record_v2_tests {
    //! `authenticated-acceptance-record/2` (M4-01, I-5). Test keys only.

    use super::tests::{good_record, sign, RECORD_KEY};
    use super::*;
    use crate::keys::{AcceptanceRecordKey, MemoryKeys};
    use writ_kernel::json::parse;

    pub(super) fn keys() -> MemoryKeys<AcceptanceRecordKey> {
        let mut k = MemoryKeys::new();
        k.register(
            "acc-key-1",
            AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
        );
        k
    }

    fn obj(v: &mut Value) -> &mut Map {
        match v {
            Value::Obj(m) => m,
            _ => panic!("not an object"),
        }
    }

    fn reseal(record: &mut Value) {
        let d = json::digest(&record.without("record_digest")).unwrap();
        obj(record).insert("record_digest".into(), json::s(d.as_str()));
    }

    /// The `/1` good record (unsigned is fine: it is re-signed) with the
    /// frozen-fixture human criterion `signoff` (`approval-service`, trust
    /// `H`) added.
    pub(super) fn with_human(v1: &Value) -> Value {
        let mut doc = v1.clone();
        let record = obj(&mut doc).get_mut("record").unwrap();
        let human = parse(br#"{"criterion_id":"signoff","mandatory":true,"required_class":"human","status":"pass","evaluator_id":"approval-service","evaluator_trust":"H","fresh_until":"2026-11-07T11:30:00Z"}"#).unwrap();
        if let Some(Value::Arr(c)) = obj(record).get_mut("criteria") {
            c.push(human);
        }
        reseal(record);
        let mut eval = obj(&mut doc).get("evaluations").unwrap().as_arr().unwrap()[1].clone();
        let e = obj(&mut eval);
        e.insert("criterion_id".into(), json::s("signoff"));
        e.insert("evaluation_id".into(), json::s("eval-signoff"));
        e.insert("evaluator_id".into(), json::s("approval-service"));
        if let Some(Value::Arr(evals)) = obj(&mut doc).get_mut("evaluations") {
            evals.push(eval);
        }
        doc
    }

    /// Upgrade a `/1` document to `/2`: `signoff_id` on every evaluation,
    /// `so-1` for the human criterion (the MAC is not recomputed).
    fn to_v2(v1: &Value) -> Value {
        let mut doc = v1.clone();
        let m = obj(&mut doc);
        m.insert("contract_version".into(), json::s(RECORD_CONTRACT_V2));
        if let Some(Value::Arr(evals)) = m.get_mut("evaluations") {
            for e in evals {
                let human = e.get("criterion_id").and_then(Value::as_str) == Some("signoff");
                let id = if human { json::s("so-1") } else { Value::Null };
                obj(e).insert("signoff_id".into(), id);
            }
        }
        doc
    }

    fn set_eval(doc: &Value, index: usize, key: &str, v: Option<Value>) -> Value {
        let mut out = doc.clone();
        if let Some(Value::Arr(evals)) = obj(&mut out).get_mut("evaluations") {
            let e = obj(&mut evals[index]);
            match v {
                Some(v) => e.insert(key.into(), v),
                None => e.remove(key),
            };
        }
        out
    }

    pub(super) fn signed(doc: &Value) -> Value {
        sign(doc, &RECORD_KEY)
    }

    pub(super) fn v2_record() -> Value {
        signed(&to_v2(&with_human(&good_record())))
    }

    pub(super) fn sha(c: char) -> String {
        format!("sha256:{}", c.to_string().repeat(64))
    }

    pub(super) fn current() -> CurrentState {
        let text = format!(
            r#"{{"tenant_id":"t1","project_id":"p1","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":7,"now":"2026-11-07T11:00:00Z","consumed_record_ids":[],"revoked_evaluator_ids":[]}}"#,
            sha('b'),
            sha('c')
        );
        CurrentState::from_value(&parse(text.as_bytes()).unwrap()).unwrap()
    }

    pub(super) fn human_requirements() -> AcceptanceRequirements {
        let text = format!(
            r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}},{{"criterion_id":"signoff","required_class":"human"}}]}}"#,
            sha('c')
        );
        AcceptanceRequirements::from_value(&parse(text.as_bytes()).unwrap()).unwrap()
    }

    fn use_check(verified: &VerifiedRecord) -> Result<Vec<String>, AcceptanceCode> {
        let current = current();
        let requirements = human_requirements();
        let registry = |_: &str| None;
        check_use(
            verified,
            &UseContext {
                current: &current,
                requirements: &requirements,
                registry: &registry,
                attestations: &[],
                consumed_attestations: &BTreeSet::new(),
                signoffs: &[],
                promotion_parties: &[],
            },
        )
        .map(|c| c.attestation_ids)
    }

    #[test]
    fn a_v2_record_names_its_signoff_exactly_for_trust_h() {
        let v = verify_record(&v2_record(), &keys()).unwrap();
        assert_eq!(v.version(), RecordVersion::V2);
        assert_eq!(v.signoff_ids(), ["so-1"]);
        assert_eq!(v.attestation_ids(), ["att-1"]);
        assert!(mac_bytes(&v2_record())
            .unwrap()
            .contains(r#""contract_version":"authenticated-acceptance-record/2""#));
        // A mechanical-only /2 record is valid with no signoff.
        let mech = verify_record(&signed(&to_v2(&good_record())), &keys()).unwrap();
        assert_eq!(mech.version(), RecordVersion::V2);
        assert!(mech.signoff_ids().is_empty());
    }

    #[test]
    fn v2_field_rules_are_enforced() {
        let doc = to_v2(&with_human(&good_record()));
        assert!(verify_record(&signed(&doc), &keys()).is_ok());
        let malformed = Err(AcceptanceCode::RecordMalformed);
        let bad = [
            // Trust H without a signoff.
            set_eval(&doc, 2, "signoff_id", Some(Value::Null)),
            // Trust M or E with a signoff.
            set_eval(&doc, 0, "signoff_id", Some(json::s("so-2"))),
            set_eval(&doc, 1, "signoff_id", Some(json::s("so-2"))),
            // The member is required and the object closed.
            set_eval(&doc, 0, "signoff_id", None),
            set_eval(&doc, 2, "signoff_id", None),
            set_eval(&doc, 0, "extra", Some(Value::Null)),
            // Malformed ids.
            set_eval(&doc, 2, "signoff_id", Some(json::s(""))),
            set_eval(&doc, 2, "signoff_id", Some(json::s("So-1"))),
            set_eval(&doc, 2, "signoff_id", Some(Value::Int(1))),
            // The trust M rule still holds.
            set_eval(&doc, 0, "attestation_id", Some(Value::Null)),
            set_eval(&doc, 2, "attestation_id", Some(json::s("att-9"))),
        ];
        for (i, d) in bad.iter().enumerate() {
            assert_eq!(
                verify_record(&signed(d), &keys()).map(|_| ()),
                malformed,
                "case {i}"
            );
        }
        let mut other = doc.clone();
        obj(&mut other).insert(
            "contract_version".into(),
            json::s("authenticated-acceptance-record/3"),
        );
        assert_eq!(
            verify_record(&signed(&other), &keys()).map(|_| ()),
            malformed
        );
    }

    #[test]
    fn v1_records_never_gain_human_authority() {
        // A /1 record with a human criterion still verifies, names no signoff,
        // and is refused at use time.
        let v1 = signed(&with_human(&good_record()));
        let v = verify_record(&v1, &keys()).unwrap();
        assert_eq!(v.version(), RecordVersion::V1);
        assert!(v.signoff_ids().is_empty());
        assert!(v.evaluations().iter().all(|e| e.signoff_id.is_none()));
        assert_eq!(use_check(&v), Err(AcceptanceCode::HumanUnavailable));
        // A /1 record cannot carry a signoff reference.
        let with_ref = set_eval(
            &with_human(&good_record()),
            2,
            "signoff_id",
            Some(json::s("so-1")),
        );
        assert_eq!(
            verify_record(&signed(&with_ref), &keys()).map(|_| ()),
            Err(AcceptanceCode::RecordMalformed)
        );
        // A signed /1 record relabelled as /2 fails the MAC: no reinterpretation.
        assert_eq!(
            verify_record(&to_v2(&v1), &keys()).map(|_| ()),
            Err(AcceptanceCode::RecordUnauthenticated)
        );
        // Mechanical-only /1 records stay valid.
        let mech = verify_record(&good_record(), &keys()).unwrap();
        assert_eq!(mech.version(), RecordVersion::V1);
        assert!(mech.signoff_ids().is_empty());
    }

    #[test]
    fn a_v2_human_record_without_registry_or_signoff_is_refused() {
        // A /2 record is no longer refused wholesale: it reaches the per-criterion
        // checks (here the empty registry refuses first; the signoff checks are in
        // `signoff_use_tests`).
        let v = verify_record(&v2_record(), &keys()).unwrap();
        assert_eq!(use_check(&v), Err(AcceptanceCode::EvaluatorUnregistered));
    }

    /// The checked-in example of
    /// `docs/contracts/proposed/authenticated-acceptance-record-2.schema.json`
    /// (validated by `test_proposed_contracts.py`) is exactly the record this
    /// code produces, and the verifier accepts it.
    #[test]
    fn the_v2_schema_example_is_the_record_the_code_produces_and_verifies() {
        let text = include_str!(
            "../../docs/contracts/proposed/examples/authenticated-acceptance-record-2.valid.json"
        );
        let example = parse(text.as_bytes()).unwrap();
        let produced = json::canonical(&v2_record()).unwrap();
        assert_eq!(json::canonical(&example).unwrap(), produced, "{produced}");
        assert_eq!(
            verify_record(&example, &keys()).unwrap().signoff_ids(),
            ["so-1"]
        );
    }
}
