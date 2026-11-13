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
//! replay of the record); human criteria are unavailable until authenticated
//! signoff exists (M4); every required criterion's evaluator must have an
//! active registry entry whose scope contains the tenant and project and
//! whose class, trust, version, digests, registry revision and criteria
//! match the binding; a mechanical criterion needs its verified attestation,
//! bound to exactly this record, criterion, transition, artifact, policy,
//! revision, evaluator and registered command, signed with the registry
//! entry's key id, exit status 0, fresh at trusted time, never consumed
//! before. Evidence-class criteria rely on the authenticated record and the
//! registry; resolving their evidence digests against a catalog is M7.
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

use crate::contracts::{RegistryEntry, RunnerAttestation};
use crate::keys::{AcceptanceKeyRing, KeyStatus, RunnerKeyRing};
use crate::mac;

/// `contract_version` of an authenticated acceptance record.
pub const RECORD_CONTRACT: &str = "authenticated-acceptance-record/1";
/// `purpose` of an authenticated acceptance record (domain separation).
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
}

/// A record whose envelope, MAC, projection and bindings verified. Only
/// [`verify_record`] builds one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedRecord {
    record: AcceptanceRecord,
    evaluations: Vec<EvaluationBinding>,
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
    if !constant("contract_version", RECORD_CONTRACT) || !constant("purpose", RECORD_PURPOSE) {
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
    let evaluations = evaluations(m.get("evaluations"), &record).ok_or(RecordMalformed)?;
    if mode == Some("advisory") {
        return Err(Advisory);
    }
    Ok(VerifiedRecord {
        record,
        evaluations,
        issuer: issuer.to_string(),
        key_id: key_id.to_string(),
    })
}

/// The evaluation bindings: exactly one per record criterion, same evaluator,
/// an attestation exactly for trust `M`.
fn evaluations(v: Option<&Value>, record: &AcceptanceRecord) -> Option<Vec<EvaluationBinding>> {
    let items = v?.as_arr()?;
    if items.len() != record.criteria.len() {
        return None;
    }
    let mut seen = BTreeSet::new();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let e = closed(item, &EVALUATION)?;
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

/// The use-time checks (module documentation), in order: kernel check, human
/// criteria, registry binding per required criterion, attestation per
/// mechanical criterion. Returns the attestation ids to consume atomically
/// with the promotion.
pub fn check_use(
    verified: &VerifiedRecord,
    ctx: &UseContext<'_>,
) -> Result<Vec<String>, AcceptanceCode> {
    use AcceptanceCode::*;
    let record = &verified.record;
    check(record, ctx.current, ctx.requirements).map_err(Kernel)?;
    if ctx
        .requirements
        .criteria
        .iter()
        .any(|c| c.required_class == CriterionClass::Human)
    {
        return Err(HumanUnavailable);
    }
    let now = ctx.current.now;
    let mut consume = Vec::new();
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
    Ok(consume)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
