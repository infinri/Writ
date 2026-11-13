//! Semantic validation of the proposed M3-06 acceptance contracts (D-033 item
//! 4; owner decision of 2026-11-12): evaluator registry entry, runner
//! attestation and EvaluationRecord scope.
//!
//! The JSON Schemas in `docs/contracts/proposed/` are a structural check only.
//! Every trusted ingestion path of these contracts goes through this module,
//! and the only way to obtain a [`RegistryEntry`], [`RunnerAttestation`] or
//! [`EvaluationRecordScope`] is its `validate` constructor, so a consumer that
//! takes one of those types cannot receive an unvalidated document. Validation
//! covers, in this order:
//!
//! 1. the closed shape of the schema: an object with no unknown members, every
//!    required member present, exact types, patterns, enums, ranges, array
//!    rules and the conditional requirements;
//! 2. every timestamp parses with the kernel's strict rules
//!    ([`Timestamp::parse_strict`]: a real calendar time, `YYYY-MM-DDTHH:MM:SSZ`);
//! 3. `canonical_target` is a canonical resource id per the kernel
//!    ([`is_canonical_resource_id`]: no `.` or `..` segment) and equals
//!    `workflow:<tenant_id>/<project_id>/<workflow_id>` of the same document;
//! 4. temporal relationships: `started_at <= finished_at <= fresh_until`,
//!    `issued_at <= fresh_until`, `revoked_at >= published_at`, and
//!    observations are not in the future relative to the trusted `now` the
//!    caller passes explicitly (`finished_at <= now`, `issued_at <= now`).
//!
//! The first failed rule is returned as a [`ContractError`]; validation never
//! panics. Not covered here (M3-05 and M3-07): MAC verification, registry
//! lookup, binding equality against the protected transition at use time,
//! freshness at use time, and consumption.
//!
//! **`evaluation-record-scope/1` is superseded for the authorization path**
//! (D-038 item 1, OQ-13 option A): [`EvaluationRecordScope`] has no ingestion
//! path, source, key or consumer and gets none. The authoritative evaluation
//! data are the evaluations inside the authenticated AcceptanceRecord
//! ([`crate::acceptance::verify_record`]), with the verified attestations and
//! current registry entries ([`crate::acceptance::check_use`]); the validator
//! and its tests are kept as history (enforcement map:
//! docs/implementation/M3-ACCEPTANCE-CONTRACTS.md).

use std::collections::BTreeSet;
use std::fmt;

use writ_kernel::decision::{in_scope, is_canonical_resource_id};
use writ_kernel::ids::{is_digest, is_id, is_name};
use writ_kernel::json::{Map, Value};
use writ_kernel::time::Timestamp;
use writ_kernel::transition::workflow_id;

/// Largest revision (2^53 - 1).
const MAX_REVISION: i64 = 9_007_199_254_740_991;

/// The contract a document was validated as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Contract {
    /// `evaluator-registry-entry/1`.
    RegistryEntry,
    /// `runner-attestation/1`.
    RunnerAttestation,
    /// `evaluation-record-scope/1`.
    EvaluationRecordScope,
}

impl Contract {
    /// The contract's `contract_version`.
    pub fn version(self) -> &'static str {
        match self {
            Contract::RegistryEntry => "evaluator-registry-entry/1",
            Contract::RunnerAttestation => "runner-attestation/1",
            Contract::EvaluationRecordScope => "evaluation-record-scope/1",
        }
    }
}

/// The rule a document failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rule {
    /// The value is not an object.
    NotAnObject,
    /// A member the contract does not define (closed objects).
    UnknownMember,
    /// A required member is absent.
    MissingMember,
    /// A member has the wrong JSON type.
    WrongType,
    /// A constant member (`contract_version`, `purpose`) has another value.
    ConstMismatch,
    /// Not `^[a-z0-9][a-z0-9_-]{0,63}$`.
    InvalidId,
    /// Not `^[a-z][a-z0-9_-]{0,63}$`.
    InvalidName,
    /// Not `^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$`.
    InvalidVersion,
    /// Not `sha256:` plus 64 lowercase hex.
    InvalidDigest,
    /// Not 64 lowercase hex.
    InvalidMac,
    /// Not one of the contract's enum values.
    NotInEnum,
    /// An integer outside its range.
    OutOfRange,
    /// An array with fewer items than required.
    TooFewItems,
    /// An array with a repeated item.
    DuplicateItem,
    /// Not a strict Writ timestamp of a real calendar time.
    InvalidTimestamp,
    /// `canonical_target` is not a canonical resource id (structure, `.` or `..`).
    InvalidCanonicalTarget,
    /// `canonical_target` is not `workflow:<tenant_id>/<project_id>/<workflow_id>`
    /// of the same document.
    CanonicalTargetMismatch,
    /// `trust` does not match `class` (mechanical M, evidence E, model_assisted MA, human H).
    ClassTrustMismatch,
    /// A mechanical evaluator without a registered `command`.
    CommandRequired,
    /// `status` is `revoked` without `revoked_at`.
    RevokedAtRequired,
    /// `revoked_at` on an entry that is not revoked.
    RevokedAtForbidden,
    /// `revoked_at` is before `published_at`.
    RevokedBeforePublished,
    /// `evaluator_trust` M without `attestation_ref`.
    AttestationRefRequired,
    /// `started_at` is after `finished_at`.
    StartedAfterFinished,
    /// `finished_at` is after `fresh_until`.
    FinishedAfterFreshUntil,
    /// `issued_at` is after `fresh_until`.
    IssuedAfterFreshUntil,
    /// `finished_at` is after the trusted `now`.
    FinishedInFuture,
    /// `issued_at` is after the trusted `now`.
    IssuedInFuture,
}

impl Rule {
    /// Stable code of the rule (audit records and tests).
    pub fn code(self) -> &'static str {
        match self {
            Rule::NotAnObject => "NOT_AN_OBJECT",
            Rule::UnknownMember => "UNKNOWN_MEMBER",
            Rule::MissingMember => "MISSING_MEMBER",
            Rule::WrongType => "WRONG_TYPE",
            Rule::ConstMismatch => "CONST_MISMATCH",
            Rule::InvalidId => "INVALID_ID",
            Rule::InvalidName => "INVALID_NAME",
            Rule::InvalidVersion => "INVALID_VERSION",
            Rule::InvalidDigest => "INVALID_DIGEST",
            Rule::InvalidMac => "INVALID_MAC",
            Rule::NotInEnum => "NOT_IN_ENUM",
            Rule::OutOfRange => "OUT_OF_RANGE",
            Rule::TooFewItems => "TOO_FEW_ITEMS",
            Rule::DuplicateItem => "DUPLICATE_ITEM",
            Rule::InvalidTimestamp => "INVALID_TIMESTAMP",
            Rule::InvalidCanonicalTarget => "INVALID_CANONICAL_TARGET",
            Rule::CanonicalTargetMismatch => "CANONICAL_TARGET_MISMATCH",
            Rule::ClassTrustMismatch => "CLASS_TRUST_MISMATCH",
            Rule::CommandRequired => "COMMAND_REQUIRED",
            Rule::RevokedAtRequired => "REVOKED_AT_REQUIRED",
            Rule::RevokedAtForbidden => "REVOKED_AT_FORBIDDEN",
            Rule::RevokedBeforePublished => "REVOKED_BEFORE_PUBLISHED",
            Rule::AttestationRefRequired => "ATTESTATION_REF_REQUIRED",
            Rule::StartedAfterFinished => "STARTED_AFTER_FINISHED",
            Rule::FinishedAfterFreshUntil => "FINISHED_AFTER_FRESH_UNTIL",
            Rule::IssuedAfterFreshUntil => "ISSUED_AFTER_FRESH_UNTIL",
            Rule::FinishedInFuture => "FINISHED_IN_FUTURE",
            Rule::IssuedInFuture => "ISSUED_IN_FUTURE",
        }
    }
}

/// A document failed validation: which contract, which member (a dotted path,
/// `[i]` for array items, empty for the document itself) and which rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractError {
    /// The contract being validated.
    pub contract: Contract,
    /// The member that failed.
    pub path: String,
    /// The rule it failed.
    pub rule: Rule,
}

impl fmt::Display for ContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}: {}: {}",
            self.contract.version(),
            self.path,
            self.rule.code()
        )
    }
}

impl std::error::Error for ContractError {}

/// The type and pattern of one member.
#[derive(Clone, Copy)]
enum Kind {
    Const(&'static str),
    Id,
    Name,
    Version,
    Digest,
    Mac,
    Timestamp,
    Revision,
    ExitStatus,
    Enum(&'static [&'static str]),
    Target,
    Digests { min: usize },
    Names { min: usize },
    Object(&'static [Field]),
}

#[derive(Clone, Copy)]
struct Field {
    name: &'static str,
    kind: Kind,
    required: bool,
}

const fn req(name: &'static str, kind: Kind) -> Field {
    Field {
        name,
        kind,
        required: true,
    }
}

const fn opt(name: &'static str, kind: Kind) -> Field {
    Field {
        name,
        kind,
        required: false,
    }
}

const TRUST: &[&str] = &["M", "E", "MA", "H"];

const TRANSITION: &[Field] = &[req("from_state", Kind::Name), req("to_state", Kind::Name)];

const REGISTRY_ENTRY: &[Field] = &[
    req(
        "contract_version",
        Kind::Const("evaluator-registry-entry/1"),
    ),
    req("evaluator_id", Kind::Name),
    req("evaluator_version", Kind::Version),
    req(
        "class",
        Kind::Enum(&["mechanical", "evidence", "model_assisted", "human"]),
    ),
    req("trust", Kind::Enum(TRUST)),
    req("implementation_digest", Kind::Digest),
    req("environment_digest", Kind::Digest),
    opt(
        "command",
        Kind::Object(&[
            req("command_id", Kind::Name),
            req("command_digest", Kind::Digest),
        ]),
    ),
    req(
        "attestation",
        Kind::Object(&[
            req("alg", Kind::Enum(&["hmac-sha256"])),
            req("key_id", Kind::Id),
        ]),
    ),
    req("criteria", Kind::Names { min: 1 }),
    req(
        "scope",
        Kind::Object(&[req("tenant_id", Kind::Id), req("project_id", Kind::Id)]),
    ),
    req("owner", Kind::Name),
    req("publisher", Kind::Name),
    req("published_at", Kind::Timestamp),
    req("registry_revision", Kind::Revision),
    req("status", Kind::Enum(&["active", "revoked"])),
    opt("revoked_at", Kind::Timestamp),
];

const RUNNER_ATTESTATION: &[Field] = &[
    req("contract_version", Kind::Const("runner-attestation/1")),
    req("purpose", Kind::Const("writ.runner-attestation.v1")),
    req("attestation_id", Kind::Id),
    req("tenant_id", Kind::Id),
    req("project_id", Kind::Id),
    req("task_id", Kind::Id),
    req("workflow_id", Kind::Name),
    req("transition", Kind::Object(TRANSITION)),
    req("artifact_snapshot_id", Kind::Id),
    req("artifact_digest", Kind::Digest),
    req("canonical_target", Kind::Target),
    req("acceptance_policy_id", Kind::Id),
    req("acceptance_policy_version", Kind::Version),
    req("acceptance_policy_bundle_sha", Kind::Digest),
    req("state_revision", Kind::Revision),
    req("acceptance_record_id", Kind::Id),
    req("criterion_id", Kind::Name),
    req("evaluator_id", Kind::Name),
    req("evaluator_version", Kind::Version),
    req("implementation_digest", Kind::Digest),
    req("environment_digest", Kind::Digest),
    req("registry_revision", Kind::Revision),
    req("command_id", Kind::Name),
    req("command_digest", Kind::Digest),
    req("input_digests", Kind::Digests { min: 1 }),
    req("evidence_digests", Kind::Digests { min: 0 }),
    req("exit_status", Kind::ExitStatus),
    req("output_digest", Kind::Digest),
    req("started_at", Kind::Timestamp),
    req("finished_at", Kind::Timestamp),
    req("fresh_until", Kind::Timestamp),
    req(
        "auth",
        Kind::Object(&[
            req("alg", Kind::Enum(&["hmac-sha256"])),
            req("key_id", Kind::Id),
            req("mac", Kind::Mac),
        ]),
    ),
];

const EVALUATION_RECORD_SCOPE: &[Field] = &[
    req("contract_version", Kind::Const("evaluation-record-scope/1")),
    req("evaluation_id", Kind::Id),
    req("tenant_id", Kind::Id),
    req("project_id", Kind::Id),
    req("task_id", Kind::Id),
    req("workflow_id", Kind::Name),
    req("transition", Kind::Object(TRANSITION)),
    req("artifact_snapshot_id", Kind::Id),
    req("artifact_digest", Kind::Digest),
    req("canonical_target", Kind::Target),
    req("acceptance_policy_id", Kind::Id),
    req("acceptance_policy_version", Kind::Version),
    req("acceptance_policy_bundle_sha", Kind::Digest),
    req("state_revision", Kind::Revision),
    req("acceptance_record_id", Kind::Id),
    req("criterion_id", Kind::Name),
    req("evaluator_id", Kind::Name),
    req("evaluator_version", Kind::Version),
    req("implementation_digest", Kind::Digest),
    req("environment_digest", Kind::Digest),
    req("registry_revision", Kind::Revision),
    req("evaluator_trust", Kind::Enum(TRUST)),
    req("status", Kind::Enum(&["pass", "fail", "unknown", "stale"])),
    opt("attestation_ref", Kind::Id),
    req("evidence_digests", Kind::Digests { min: 0 }),
    req("issued_at", Kind::Timestamp),
    req("fresh_until", Kind::Timestamp),
    req("mode", Kind::Enum(&["governed", "advisory"])),
];

/// `^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$`: evaluator and acceptance policy versions.
fn is_version(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => {
            s.len() <= 64
                && first.is_ascii_alphanumeric()
                && tail
                    .iter()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
        }
        None => false,
    }
}

/// `^[0-9a-f]{64}$`.
fn is_mac(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// `^[a-z][a-z0-9_-]*$`: the scheme of a canonical target.
fn is_scheme(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => {
            first.is_ascii_lowercase()
                && tail.iter().all(|c| {
                    c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'_' | b'-')
                })
        }
        None => false,
    }
}

/// The schema pattern of `canonical_target` (scheme, tenant id, project id,
/// segments of `[A-Za-z0-9._-]+` that are not `.` or `..`) and the kernel's
/// canonical resource id rules.
fn is_canonical_target(s: &str) -> bool {
    let Some((scheme, rest)) = s.split_once(':') else {
        return false;
    };
    let mut segments = rest.split('/');
    let (Some(tenant), Some(project)) = (segments.next(), segments.next()) else {
        return false;
    };
    is_scheme(scheme)
        && is_id(tenant)
        && is_id(project)
        && segments.all(|seg| {
            !seg.is_empty()
                && seg != "."
                && seg != ".."
                && seg
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
        })
        && is_canonical_resource_id(s)
}

struct Checker {
    contract: Contract,
}

impl Checker {
    fn fail(&self, path: &str, rule: Rule) -> ContractError {
        ContractError {
            contract: self.contract,
            path: path.to_string(),
            rule,
        }
    }

    fn object<'v>(
        &self,
        value: &'v Value,
        path: &str,
        fields: &[Field],
    ) -> Result<&'v Map, ContractError> {
        let members = value
            .as_obj()
            .ok_or_else(|| self.fail(path, Rule::NotAnObject))?;
        if let Some(unknown) = members
            .keys()
            .find(|k| !fields.iter().any(|f| f.name == k.as_str()))
        {
            return Err(self.fail(&join(path, unknown), Rule::UnknownMember));
        }
        for field in fields {
            let at = join(path, field.name);
            match members.get(field.name) {
                Some(v) => self.member(v, &at, field.kind)?,
                None if field.required => return Err(self.fail(&at, Rule::MissingMember)),
                None => {}
            }
        }
        Ok(members)
    }

    fn text<'v>(&self, value: &'v Value, path: &str) -> Result<&'v str, ContractError> {
        value
            .as_str()
            .ok_or_else(|| self.fail(path, Rule::WrongType))
    }

    fn pattern(
        &self,
        value: &Value,
        path: &str,
        ok: fn(&str) -> bool,
        rule: Rule,
    ) -> Result<(), ContractError> {
        if ok(self.text(value, path)?) {
            Ok(())
        } else {
            Err(self.fail(path, rule))
        }
    }

    fn integer(&self, value: &Value, path: &str, max: i64) -> Result<(), ContractError> {
        let n = value
            .as_int()
            .ok_or_else(|| self.fail(path, Rule::WrongType))?;
        if (0..=max).contains(&n) {
            Ok(())
        } else {
            Err(self.fail(path, Rule::OutOfRange))
        }
    }

    fn array(
        &self,
        value: &Value,
        path: &str,
        min: usize,
        ok: fn(&str) -> bool,
        rule: Rule,
    ) -> Result<(), ContractError> {
        let items = value
            .as_arr()
            .ok_or_else(|| self.fail(path, Rule::WrongType))?;
        let mut seen = BTreeSet::new();
        for (i, item) in items.iter().enumerate() {
            let at = format!("{path}[{i}]");
            self.pattern(item, &at, ok, rule)?;
            if !seen.insert(self.text(item, &at)?) {
                return Err(self.fail(&at, Rule::DuplicateItem));
            }
        }
        if items.len() < min {
            return Err(self.fail(path, Rule::TooFewItems));
        }
        Ok(())
    }

    fn member(&self, value: &Value, path: &str, kind: Kind) -> Result<(), ContractError> {
        match kind {
            Kind::Const(expected) => {
                if self.text(value, path)? == expected {
                    Ok(())
                } else {
                    Err(self.fail(path, Rule::ConstMismatch))
                }
            }
            Kind::Id => self.pattern(value, path, is_id, Rule::InvalidId),
            Kind::Name => self.pattern(value, path, is_name, Rule::InvalidName),
            Kind::Version => self.pattern(value, path, is_version, Rule::InvalidVersion),
            Kind::Digest => self.pattern(value, path, is_digest, Rule::InvalidDigest),
            Kind::Mac => self.pattern(value, path, is_mac, Rule::InvalidMac),
            Kind::Timestamp => self.pattern(
                value,
                path,
                |s| Timestamp::parse_strict(s).is_some(),
                Rule::InvalidTimestamp,
            ),
            Kind::Revision => self.integer(value, path, MAX_REVISION),
            Kind::ExitStatus => self.integer(value, path, 255),
            Kind::Enum(values) => {
                if values.contains(&self.text(value, path)?) {
                    Ok(())
                } else {
                    Err(self.fail(path, Rule::NotInEnum))
                }
            }
            Kind::Target => self.pattern(
                value,
                path,
                is_canonical_target,
                Rule::InvalidCanonicalTarget,
            ),
            Kind::Digests { min } => self.array(value, path, min, is_digest, Rule::InvalidDigest),
            Kind::Names { min } => self.array(value, path, min, is_name, Rule::InvalidName),
            Kind::Object(fields) => self.object(value, path, fields).map(|_| ()),
        }
    }

    /// A member already checked as present and well formed (a structural miss
    /// here is still a typed error, never a panic).
    fn str_of<'v>(&self, members: &'v Map, name: &str) -> Result<&'v str, ContractError> {
        members
            .get(name)
            .and_then(Value::as_str)
            .ok_or_else(|| self.fail(name, Rule::MissingMember))
    }

    fn time_of(&self, members: &Map, name: &str) -> Result<Timestamp, ContractError> {
        Timestamp::parse_strict(self.str_of(members, name)?)
            .ok_or_else(|| self.fail(name, Rule::InvalidTimestamp))
    }

    fn not_after(
        &self,
        members: &Map,
        earlier: &str,
        later: &str,
        rule: Rule,
    ) -> Result<(), ContractError> {
        if self.time_of(members, earlier)? <= self.time_of(members, later)? {
            Ok(())
        } else {
            Err(self.fail(earlier, rule))
        }
    }

    fn not_in_future(
        &self,
        members: &Map,
        name: &str,
        now: Timestamp,
        rule: Rule,
    ) -> Result<(), ContractError> {
        if self.time_of(members, name)? <= now {
            Ok(())
        } else {
            Err(self.fail(name, rule))
        }
    }

    /// `canonical_target` is `workflow:<tenant_id>/<project_id>/<workflow_id>`.
    fn target_binding(&self, members: &Map) -> Result<(), ContractError> {
        let target = self.str_of(members, "canonical_target")?;
        let tenant = self.str_of(members, "tenant_id")?;
        let project = self.str_of(members, "project_id")?;
        let workflow = self.str_of(members, "workflow_id")?;
        let bound = in_scope(target, tenant, project)
            && workflow_id(target, tenant, project).as_deref() == Some(workflow)
            && target == format!("workflow:{tenant}/{project}/{workflow}");
        if bound {
            Ok(())
        } else {
            Err(self.fail("canonical_target", Rule::CanonicalTargetMismatch))
        }
    }
}

fn join(path: &str, name: &str) -> String {
    if path.is_empty() {
        name.to_string()
    } else {
        format!("{path}.{name}")
    }
}

/// An evaluator registry entry that passed semantic validation. Not looked up
/// or authorized (M3-05).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegistryEntry(Value);

impl RegistryEntry {
    /// Validate `value` as an `evaluator-registry-entry/1`.
    pub fn validate(value: &Value) -> Result<RegistryEntry, ContractError> {
        let c = Checker {
            contract: Contract::RegistryEntry,
        };
        let m = c.object(value, "", REGISTRY_ENTRY)?;
        let class = c.str_of(m, "class")?;
        let expected_trust = match class {
            "mechanical" => "M",
            "evidence" => "E",
            "model_assisted" => "MA",
            _ => "H",
        };
        if c.str_of(m, "trust")? != expected_trust {
            return Err(c.fail("trust", Rule::ClassTrustMismatch));
        }
        if class == "mechanical" && !m.contains_key("command") {
            return Err(c.fail("command", Rule::CommandRequired));
        }
        match (c.str_of(m, "status")?, m.contains_key("revoked_at")) {
            ("revoked", false) => return Err(c.fail("revoked_at", Rule::RevokedAtRequired)),
            ("revoked", true) => {
                if c.time_of(m, "revoked_at")? < c.time_of(m, "published_at")? {
                    return Err(c.fail("revoked_at", Rule::RevokedBeforePublished));
                }
            }
            (_, true) => return Err(c.fail("revoked_at", Rule::RevokedAtForbidden)),
            (_, false) => {}
        }
        Ok(RegistryEntry(value.clone()))
    }

    /// The validated document.
    pub fn as_value(&self) -> &Value {
        &self.0
    }
}

/// A runner attestation that passed semantic validation. Its MAC is not
/// verified (M3-07) and it is not consumed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunnerAttestation(Value);

impl RunnerAttestation {
    /// Validate `value` as a `runner-attestation/1` at the trusted time `now`.
    pub fn validate(value: &Value, now: Timestamp) -> Result<RunnerAttestation, ContractError> {
        let c = Checker {
            contract: Contract::RunnerAttestation,
        };
        let m = c.object(value, "", RUNNER_ATTESTATION)?;
        c.target_binding(m)?;
        c.not_after(m, "started_at", "finished_at", Rule::StartedAfterFinished)?;
        c.not_after(
            m,
            "finished_at",
            "fresh_until",
            Rule::FinishedAfterFreshUntil,
        )?;
        c.not_in_future(m, "finished_at", now, Rule::FinishedInFuture)?;
        Ok(RunnerAttestation(value.clone()))
    }

    /// The validated document.
    pub fn as_value(&self) -> &Value {
        &self.0
    }
}

/// An EvaluationRecord scope binding that passed semantic validation.
/// Superseded for the authorization path (D-038 item 1): never ingested and
/// never an authorization input; kept as history.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvaluationRecordScope(Value);

impl EvaluationRecordScope {
    /// Validate `value` as an `evaluation-record-scope/1` at the trusted time `now`.
    pub fn validate(value: &Value, now: Timestamp) -> Result<EvaluationRecordScope, ContractError> {
        let c = Checker {
            contract: Contract::EvaluationRecordScope,
        };
        let m = c.object(value, "", EVALUATION_RECORD_SCOPE)?;
        if c.str_of(m, "evaluator_trust")? == "M" && !m.contains_key("attestation_ref") {
            return Err(c.fail("attestation_ref", Rule::AttestationRefRequired));
        }
        c.target_binding(m)?;
        c.not_after(m, "issued_at", "fresh_until", Rule::IssuedAfterFreshUntil)?;
        c.not_in_future(m, "issued_at", now, Rule::IssuedInFuture)?;
        Ok(EvaluationRecordScope(value.clone()))
    }

    /// The validated document.
    pub fn as_value(&self) -> &Value {
        &self.0
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::test_support::{with, without};
    use writ_kernel::json::{self, Value};

    fn example(text: &str) -> Value {
        json::parse(text.as_bytes()).unwrap()
    }

    fn entry() -> Value {
        example(include_str!(
            "../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
        ))
    }

    fn attestation() -> Value {
        example(include_str!(
            "../../docs/contracts/proposed/examples/runner-attestation.valid.json"
        ))
    }

    fn record() -> Value {
        example(include_str!(
            "../../docs/contracts/proposed/examples/evaluation-record-scope.valid.json"
        ))
    }

    /// Trusted time after every observation in the examples.
    fn now() -> Timestamp {
        Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
    }

    /// The failed member and rule, or `Ok`.
    type Outcome = Result<(), (String, Rule)>;

    fn entry_rule(v: &Value) -> Outcome {
        RegistryEntry::validate(v)
            .map(|_| ())
            .map_err(|e| (e.path, e.rule))
    }

    fn attestation_rule(v: &Value) -> Outcome {
        RunnerAttestation::validate(v, now())
            .map(|_| ())
            .map_err(|e| (e.path, e.rule))
    }

    fn record_rule(v: &Value) -> Outcome {
        EvaluationRecordScope::validate(v, now())
            .map(|_| ())
            .map_err(|e| (e.path, e.rule))
    }

    /// Both scoped contracts give the same result for the same mutation.
    fn scoped_rule(path: &[&str], value: Value) -> (Outcome, Outcome) {
        (
            attestation_rule(&with(&attestation(), path, value.clone())),
            record_rule(&with(&record(), path, value)),
        )
    }

    fn err(path: &str, rule: Rule) -> Outcome {
        Err((path.to_string(), rule))
    }

    #[test]
    fn valid_examples_pass() {
        let e = RegistryEntry::validate(&entry()).unwrap();
        assert_eq!(e.as_value(), &entry());
        let a = RunnerAttestation::validate(&attestation(), now()).unwrap();
        assert_eq!(a.as_value(), &attestation());
        let r = EvaluationRecordScope::validate(&record(), now()).unwrap();
        assert_eq!(r.as_value(), &record());
        // Observations exactly at `now` are not in the future.
        let at = Timestamp::parse_strict("2026-11-07T10:59:00Z").unwrap();
        assert!(RunnerAttestation::validate(&attestation(), at).is_ok());
        assert!(EvaluationRecordScope::validate(&record(), at).is_ok());
    }

    #[test]
    fn canonical_target_with_dot_segments_is_rejected() {
        for target in [
            "workflow:t1/p1/..",
            "workflow:t1/p1/.",
            "workflow:t1/p1/../wf-1",
            "workflow:t1/p1/./wf-1",
            "workflow:t1/p1/wf-1/..",
            "workflow:t1/../p1/wf-1",
        ] {
            let expected = err("canonical_target", Rule::InvalidCanonicalTarget);
            assert_eq!(
                scoped_rule(&["canonical_target"], json::s(target)),
                (expected.clone(), expected),
                "{target}"
            );
        }
    }

    #[test]
    fn canonical_target_with_invalid_structure_is_rejected() {
        for target in [
            "",
            "workflow",
            "workflow:",
            "workflow:t1",
            "workflow:t1/p1",
            "workflow:t1/p1/",
            "workflow:t1//wf-1",
            "workflow:t1/p1//wf-1",
            "workflow:T1/p1/wf-1",
            "Workflow:t1/p1/wf-1",
            ":t1/p1/wf-1",
            "workflow:t1/p1/wf 1",
            "workflow:t1/p1/wf\\1",
        ] {
            let expected = err("canonical_target", Rule::InvalidCanonicalTarget);
            assert_eq!(
                scoped_rule(&["canonical_target"], json::s(target)),
                (expected.clone(), expected),
                "{target:?}"
            );
        }
    }

    #[test]
    fn canonical_target_for_another_scope_or_workflow_is_rejected() {
        for target in [
            "workflow:t2/p1/wf-1",
            "workflow:t1/p2/wf-1",
            "workflow:t1/p1/wf-2",
            "workflow:t1/p1/wf-1/extra",
            "repository:t1/p1/wf-1",
            "workspace:t1/p1/wf-1",
        ] {
            let expected = err("canonical_target", Rule::CanonicalTargetMismatch);
            assert_eq!(
                scoped_rule(&["canonical_target"], json::s(target)),
                (expected.clone(), expected),
                "{target}"
            );
        }
        // The bound ids moved instead of the target: still a mismatch.
        for (member, value) in [
            ("tenant_id", "t2"),
            ("project_id", "p2"),
            ("workflow_id", "wf-2"),
        ] {
            let expected = err("canonical_target", Rule::CanonicalTargetMismatch);
            assert_eq!(
                scoped_rule(&[member], json::s(value)),
                (expected.clone(), expected),
                "{member}"
            );
        }
    }

    #[test]
    fn invalid_calendar_times_are_rejected() {
        for bad in [
            "2026-02-30T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-11-07T24:00:00Z",
            "2026-11-07T10:60:00Z",
            "2026-11-07T10:59:60Z",
            "2026-00-10T00:00:00Z",
            "2026-11-00T00:00:00Z",
            "2025-02-29T00:00:00Z",
            "2026-11-07T10:59:00.5Z",
            "2026-11-07T10:59:00+00:00",
            "2026-11-07t10:59:00z",
        ] {
            for member in ["started_at", "finished_at", "fresh_until"] {
                assert_eq!(
                    attestation_rule(&with(&attestation(), &[member], json::s(bad))),
                    err(member, Rule::InvalidTimestamp),
                    "{member}={bad}"
                );
            }
            for member in ["issued_at", "fresh_until"] {
                assert_eq!(
                    record_rule(&with(&record(), &[member], json::s(bad))),
                    err(member, Rule::InvalidTimestamp),
                    "{member}={bad}"
                );
            }
            assert_eq!(
                entry_rule(&with(&entry(), &["published_at"], json::s(bad))),
                err("published_at", Rule::InvalidTimestamp),
                "published_at={bad}"
            );
            let revoked = with(&entry(), &["status"], json::s("revoked"));
            assert_eq!(
                entry_rule(&with(&revoked, &["revoked_at"], json::s(bad))),
                err("revoked_at", Rule::InvalidTimestamp),
                "revoked_at={bad}"
            );
        }
        // A real leap day is a valid time.
        let leap = with(
            &attestation(),
            &["started_at"],
            json::s("2024-02-29T00:00:00Z"),
        );
        assert_eq!(attestation_rule(&leap), Ok(()));
    }

    #[test]
    fn temporal_order_of_an_attestation_is_enforced() {
        let inverted = with(
            &attestation(),
            &["started_at"],
            json::s("2026-11-07T10:59:01Z"),
        );
        assert_eq!(
            attestation_rule(&inverted),
            err("started_at", Rule::StartedAfterFinished)
        );
        let instant = with(
            &attestation(),
            &["started_at"],
            json::s("2026-11-07T10:59:00Z"),
        );
        assert_eq!(
            attestation_rule(&instant),
            Ok(()),
            "a zero-length run is ordered"
        );
        let stale_on_arrival = with(
            &attestation(),
            &["fresh_until"],
            json::s("2026-11-07T10:58:59Z"),
        );
        assert_eq!(
            attestation_rule(&stale_on_arrival),
            err("finished_at", Rule::FinishedAfterFreshUntil)
        );
        let boundary = with(
            &attestation(),
            &["fresh_until"],
            json::s("2026-11-07T10:59:00Z"),
        );
        assert_eq!(attestation_rule(&boundary), Ok(()));
    }

    #[test]
    fn future_observations_are_rejected() {
        let future = with(
            &attestation(),
            &["finished_at"],
            json::s("2026-11-07T11:00:01Z"),
        );
        assert_eq!(
            attestation_rule(&future),
            err("finished_at", Rule::FinishedInFuture)
        );
        let earlier_now = Timestamp::parse_strict("2026-11-07T10:58:59Z").unwrap();
        assert_eq!(
            RunnerAttestation::validate(&attestation(), earlier_now).map_err(|e| e.rule),
            Err(Rule::FinishedInFuture)
        );
        let future = with(&record(), &["issued_at"], json::s("2026-11-07T11:00:01Z"));
        assert_eq!(record_rule(&future), err("issued_at", Rule::IssuedInFuture));
        assert_eq!(
            EvaluationRecordScope::validate(&record(), earlier_now).map_err(|e| e.rule),
            Err(Rule::IssuedInFuture)
        );
    }

    #[test]
    fn temporal_order_of_a_record_and_registry_entry_is_enforced() {
        let stale = with(&record(), &["fresh_until"], json::s("2026-11-07T10:58:59Z"));
        assert_eq!(
            record_rule(&stale),
            err("issued_at", Rule::IssuedAfterFreshUntil)
        );
        let revoked = with(&entry(), &["status"], json::s("revoked"));
        let before = with(&revoked, &["revoked_at"], json::s("2026-10-31T23:59:59Z"));
        assert_eq!(
            entry_rule(&before),
            err("revoked_at", Rule::RevokedBeforePublished)
        );
        let same = with(&revoked, &["revoked_at"], json::s("2026-11-01T09:00:00Z"));
        assert_eq!(entry_rule(&same), Ok(()));
        let after = with(&revoked, &["revoked_at"], json::s("2026-11-08T00:00:00Z"));
        assert_eq!(entry_rule(&after), Ok(()));
    }

    #[test]
    fn closed_objects_and_required_members() {
        for (name, rule) in [
            ("extra", Rule::UnknownMember),
            ("transition.x", Rule::UnknownMember),
        ] {
            let path: Vec<&str> = name.split('.').collect();
            assert_eq!(
                attestation_rule(&with(&attestation(), &path, Value::Int(1))),
                err(name, rule)
            );
        }
        assert_eq!(
            attestation_rule(&with(&attestation(), &["auth", "sig"], json::s("x"))),
            err("auth.sig", Rule::UnknownMember)
        );
        assert_eq!(
            entry_rule(&with(&entry(), &["scope", "global"], Value::Bool(true))),
            err("scope.global", Rule::UnknownMember)
        );
        assert_eq!(
            record_rule(&with(&record(), &["extra"], Value::Null)),
            err("extra", Rule::UnknownMember)
        );
        for member in RUNNER_ATTESTATION.iter().map(|f| f.name) {
            assert_eq!(
                attestation_rule(&without(&attestation(), &[member])),
                err(member, Rule::MissingMember),
                "{member}"
            );
        }
        for member in EVALUATION_RECORD_SCOPE
            .iter()
            .filter(|f| f.required)
            .map(|f| f.name)
        {
            assert_eq!(
                record_rule(&without(&record(), &[member])),
                err(member, Rule::MissingMember),
                "{member}"
            );
        }
        for member in REGISTRY_ENTRY.iter().filter(|f| f.required).map(|f| f.name) {
            assert_eq!(
                entry_rule(&without(&entry(), &[member])),
                err(member, Rule::MissingMember),
                "{member}"
            );
        }
        assert_eq!(
            attestation_rule(&without(&attestation(), &["auth", "mac"])),
            err("auth.mac", Rule::MissingMember)
        );
        for not_object in [
            Value::Null,
            Value::Arr(vec![]),
            json::s("{}"),
            Value::Int(0),
        ] {
            assert_eq!(attestation_rule(&not_object), err("", Rule::NotAnObject));
            assert_eq!(record_rule(&not_object), err("", Rule::NotAnObject));
            assert_eq!(entry_rule(&not_object), err("", Rule::NotAnObject));
        }
        assert_eq!(
            entry_rule(&with(&entry(), &["scope"], json::s("global"))),
            err("scope", Rule::NotAnObject)
        );
    }

    #[test]
    fn field_types_patterns_and_ranges() {
        let digest_upper = format!("sha256:{}", "B".repeat(64));
        let cases: Vec<(&str, Value, Rule)> = vec![
            (
                "contract_version",
                json::s("runner-attestation/2"),
                Rule::ConstMismatch,
            ),
            ("purpose", json::s("writ.permit.v1"), Rule::ConstMismatch),
            ("attestation_id", json::s("Att-1"), Rule::InvalidId),
            ("tenant_id", json::s("t/1"), Rule::InvalidId),
            ("workflow_id", json::s("1wf"), Rule::InvalidName),
            ("criterion_id", json::s("Build"), Rule::InvalidName),
            ("evaluator_version", json::s(".1"), Rule::InvalidVersion),
            (
                "acceptance_policy_version",
                json::s(""),
                Rule::InvalidVersion,
            ),
            (
                "artifact_digest",
                json::s(&digest_upper),
                Rule::InvalidDigest,
            ),
            ("output_digest", json::s("sha1:abc"), Rule::InvalidDigest),
            ("exit_status", Value::Int(256), Rule::OutOfRange),
            ("exit_status", Value::Int(-1), Rule::OutOfRange),
            ("exit_status", json::s("0"), Rule::WrongType),
            ("state_revision", Value::Int(-1), Rule::OutOfRange),
            (
                "state_revision",
                Value::Int(MAX_REVISION + 1),
                Rule::OutOfRange,
            ),
            ("registry_revision", Value::Bool(true), Rule::WrongType),
            ("input_digests", Value::Arr(vec![]), Rule::TooFewItems),
            ("input_digests", json::s("sha256:x"), Rule::WrongType),
            ("canonical_target", Value::Int(1), Rule::WrongType),
            ("started_at", Value::Int(0), Rule::WrongType),
        ];
        for (member, value, rule) in cases {
            assert_eq!(
                attestation_rule(&with(&attestation(), &[member], value.clone())),
                err(member, rule),
                "{member}={value:?}"
            );
        }
        let d = format!("sha256:{}", "b".repeat(64));
        assert_eq!(
            attestation_rule(&with(
                &attestation(),
                &["input_digests"],
                Value::Arr(vec![json::s(&d), json::s(&d)])
            )),
            err("input_digests[1]", Rule::DuplicateItem)
        );
        assert_eq!(
            attestation_rule(&with(
                &attestation(),
                &["evidence_digests"],
                Value::Arr(vec![json::s(&digest_upper)])
            )),
            err("evidence_digests[0]", Rule::InvalidDigest)
        );
        for alg in ["hmac-sha1", "HMAC-SHA256", "ed25519", "none", ""] {
            assert_eq!(
                attestation_rule(&with(&attestation(), &["auth", "alg"], json::s(alg))),
                err("auth.alg", Rule::NotInEnum),
                "{alg}"
            );
            assert_eq!(
                entry_rule(&with(&entry(), &["attestation", "alg"], json::s(alg))),
                err("attestation.alg", Rule::NotInEnum),
                "{alg}"
            );
        }
        for mac in [
            "A".repeat(64),
            "a".repeat(63),
            "a".repeat(65),
            "zz".repeat(32),
        ] {
            assert_eq!(
                attestation_rule(&with(&attestation(), &["auth", "mac"], json::s(&mac))),
                err("auth.mac", Rule::InvalidMac),
                "{mac}"
            );
        }
        assert_eq!(
            record_rule(&with(&record(), &["mode"], json::s("verified"))),
            err("mode", Rule::NotInEnum)
        );
        assert_eq!(
            record_rule(&with(&record(), &["status"], json::s("passed"))),
            err("status", Rule::NotInEnum)
        );
        assert_eq!(
            record_rule(&with(&record(), &["evaluator_trust"], json::s("m"))),
            err("evaluator_trust", Rule::NotInEnum)
        );
        assert_eq!(
            entry_rule(&with(&entry(), &["criteria"], Value::Arr(vec![]))),
            err("criteria", Rule::TooFewItems)
        );
        assert_eq!(
            entry_rule(&with(
                &entry(),
                &["criteria"],
                Value::Arr(vec![json::s("build"), json::s("build")])
            )),
            err("criteria[1]", Rule::DuplicateItem)
        );
    }

    #[test]
    fn conditional_requirements() {
        assert_eq!(
            entry_rule(&with(&entry(), &["trust"], json::s("MA"))),
            err("trust", Rule::ClassTrustMismatch)
        );
        for (class, trust) in [("evidence", "E"), ("model_assisted", "MA"), ("human", "H")] {
            let e = with(
                &with(&entry(), &["class"], json::s(class)),
                &["trust"],
                json::s(trust),
            );
            assert_eq!(entry_rule(&e), Ok(()), "{class}");
            assert_eq!(
                entry_rule(&with(&e, &["trust"], json::s("M"))),
                err("trust", Rule::ClassTrustMismatch),
                "{class}"
            );
            assert_eq!(entry_rule(&without(&e, &["command"])), Ok(()), "{class}");
        }
        assert_eq!(
            entry_rule(&without(&entry(), &["command"])),
            err("command", Rule::CommandRequired)
        );
        assert_eq!(
            entry_rule(&with(&entry(), &["status"], json::s("revoked"))),
            err("revoked_at", Rule::RevokedAtRequired)
        );
        assert_eq!(
            entry_rule(&with(
                &entry(),
                &["revoked_at"],
                json::s("2026-11-08T00:00:00Z")
            )),
            err("revoked_at", Rule::RevokedAtForbidden)
        );
        assert_eq!(
            record_rule(&without(&record(), &["attestation_ref"])),
            err("attestation_ref", Rule::AttestationRefRequired)
        );
        let evidence = with(
            &without(&record(), &["attestation_ref"]),
            &["evaluator_trust"],
            json::s("E"),
        );
        assert_eq!(record_rule(&evidence), Ok(()));
    }

    #[test]
    fn errors_name_the_contract_member_and_rule() {
        let e = RunnerAttestation::validate(
            &with(
                &attestation(),
                &["canonical_target"],
                json::s("workflow:t1/p1/.."),
            ),
            now(),
        )
        .unwrap_err();
        assert_eq!(e.contract, Contract::RunnerAttestation);
        assert_eq!(
            e.to_string(),
            "runner-attestation/1: canonical_target: INVALID_CANONICAL_TARGET"
        );
        let e = EvaluationRecordScope::validate(
            &with(&record(), &["issued_at"], json::s("2026-02-30T00:00:00Z")),
            now(),
        )
        .unwrap_err();
        assert_eq!(
            e.to_string(),
            "evaluation-record-scope/1: issued_at: INVALID_TIMESTAMP"
        );
        assert_eq!(
            Contract::RegistryEntry.version(),
            "evaluator-registry-entry/1"
        );
    }
}
