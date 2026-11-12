//! Typed kernel contracts (M2-03).
//!
//! Each type is built from a strictly parsed [`Value`] by a `from_value`
//! constructor that enforces the corresponding schema in `docs/contracts/`
//! (ActionRequest, PolicyBundle) or the M1 acceptance contract
//! (`m1/fixtures/acceptance/SEMANTICS.md`, `m1-draft-3`). Objects are closed:
//! an unexpected member is a schema violation. String lengths count Unicode
//! scalar values, as JSON Schema does. A constructed value is valid by
//! construction; the decision and acceptance logic never re-validates shape.
//!
//! Tenant and project binding (ENT-R02): an [`ActionRequest`] carries its
//! `tenant_id` and `project_id`, and the decision kernel only lets it target a
//! canonical resource id under `<scheme>:<tenant>/<project>/` (see
//! [`crate::decision`]). Acceptance records and broker state are bound to
//! tenant, project, task and workflow, all compared exactly.

use crate::ids::{is_digest, is_id, is_kind, is_name, is_policy_version, is_reason_code};
use crate::json::{self, Digest, Map, Value, MAX_SAFE_INTEGER};
use crate::time::Timestamp;
use std::fmt;

/// `contract_version` of ActionRequest, PolicyBundle and Decision.
pub const CONTRACT_VERSION: &str = "2.0";

/// `contract_version` of acceptance records (M1 acceptance contract).
pub const ACCEPTANCE_CONTRACT_VERSION: &str = "m1-draft-3";

/// The action kind of a workflow transition.
pub const WORKFLOW_TRANSITION: &str = "workflow.transition";

/// The first schema rule a value violates, named by the path of the member
/// (for audit and diagnostics; never part of an agent-facing response).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SchemaError {
    /// Path of the violating member, for example `action.kind`.
    pub path: &'static str,
}

impl fmt::Display for SchemaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "schema violation at {}", self.path)
    }
}

impl std::error::Error for SchemaError {}

type Result<T> = std::result::Result<T, SchemaError>;

fn violation<T>(path: &'static str) -> Result<T> {
    Err(SchemaError { path })
}

/// An object whose members are all in `allowed` and include every `required`.
fn closed<'a>(
    v: &'a Value,
    allowed: &[&str],
    required: &[&str],
    path: &'static str,
) -> Result<&'a Map> {
    match v {
        Value::Obj(m)
            if m.keys().all(|k| allowed.contains(&k.as_str()))
                && required.iter().all(|k| m.contains_key(*k)) =>
        {
            Ok(m)
        }
        _ => violation(path),
    }
}

/// An object with exactly the members `keys`.
fn exact<'a>(v: &'a Value, keys: &[&str], path: &'static str) -> Result<&'a Map> {
    closed(v, keys, keys, path)
}

fn text(v: Option<&Value>, min: usize, max: usize, path: &'static str) -> Result<String> {
    match v {
        Some(Value::Str(s)) if (min..=max).contains(&s.chars().count()) => Ok(s.clone()),
        _ => violation(path),
    }
}

fn nonempty(v: Option<&Value>, path: &'static str) -> Result<String> {
    text(v, 1, usize::MAX, path)
}

fn matching(v: Option<&Value>, pattern: fn(&str) -> bool, path: &'static str) -> Result<String> {
    match v {
        Some(Value::Str(s)) if pattern(s) => Ok(s.clone()),
        _ => violation(path),
    }
}

/// Absent, `null` or a string.
fn text_or_null(v: Option<&Value>, path: &'static str) -> Result<Option<String>> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Str(s)) => Ok(Some(s.clone())),
        _ => violation(path),
    }
}

fn strings(
    v: Option<&Value>,
    min_items: usize,
    unique: bool,
    item: impl Fn(&str) -> bool,
    path: &'static str,
) -> Result<Vec<String>> {
    let Some(Value::Arr(items)) = v else {
        return violation(path);
    };
    let mut out: Vec<String> = Vec::with_capacity(items.len());
    for x in items {
        match x {
            Value::Str(s) if item(s) && !(unique && out.contains(s)) => out.push(s.clone()),
            _ => return violation(path),
        }
    }
    if out.len() < min_items {
        return violation(path);
    }
    Ok(out)
}

fn optional<T>(
    v: Option<&Value>,
    read: impl FnOnce(Option<&Value>) -> Result<T>,
) -> Result<Option<T>> {
    match v {
        None => Ok(None),
        some => read(some).map(Some),
    }
}

fn any(_: &str) -> bool {
    true
}

fn not_empty(s: &str) -> bool {
    !s.is_empty()
}

fn strict_time(v: Option<&Value>, path: &'static str) -> Result<Timestamp> {
    v.and_then(Value::as_str)
        .and_then(Timestamp::parse_strict)
        .ok_or(SchemaError { path })
}

// ===========================================================================
// State revision.

/// A workflow or kernel state revision: an integer from 0 to 2^53-1 (D-017).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct StateRevision(i64);

impl StateRevision {
    /// `Some` for 0 to 2^53-1.
    pub fn new(n: i64) -> Option<StateRevision> {
        (0..=MAX_SAFE_INTEGER)
            .contains(&n)
            .then_some(StateRevision(n))
    }

    /// The revision number.
    pub fn get(self) -> i64 {
        self.0
    }

    fn read(v: Option<&Value>, path: &'static str) -> Result<StateRevision> {
        v.and_then(Value::as_int)
            .and_then(StateRevision::new)
            .ok_or(SchemaError { path })
    }
}

// ===========================================================================
// ActionRequest (docs/contracts/action-request.schema.json).

/// The verified principal of a request. `verified_by` names the trusted
/// adapter; the kernel's issuer check is sound only behind sealed ingress (D-010).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Principal {
    /// Principal id.
    pub id: String,
    /// Issuer that verified the principal.
    pub verified_by: String,
    /// Authentication method.
    pub authn_method: String,
    /// Roles held (unique; empty when absent).
    pub roles: Vec<String>,
}

/// The resource an action targets.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resource {
    /// Host-facing URI.
    pub uri: String,
    /// Canonical resource id `<scheme>:<tenant>/<project>/<path>`, when normalized.
    pub canonical_resource_id: Option<String>,
}

/// The requested operation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Action {
    /// Operation identity (`^[a-z][a-z0-9_.-]*$`).
    pub kind: String,
    /// Target resource.
    pub resource: Resource,
    /// Digest of the normalized operation (at least 16 characters).
    pub action_digest: String,
    /// Free-form attributes; `None` when absent. Closed for `workflow.transition` (see [`crate::transition`]).
    pub attributes: Option<Map>,
    /// Additional distinct capabilities (unique; empty when absent).
    pub requested_capabilities: Vec<String>,
}

/// An ActionRequest, schema-valid by construction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionRequest {
    /// Request id (1 to 128 characters).
    pub request_id: String,
    /// Trace id (1 to 128 characters).
    pub trace_id: String,
    /// Tenant the request is bound to (1 to 128 characters).
    pub tenant_id: String,
    /// Verified principal.
    pub principal: Principal,
    /// Project the request is bound to.
    pub project_id: String,
    /// Session id.
    pub session_id: String,
    /// Agent id.
    pub agent_id: Option<String>,
    /// Parent session id.
    pub parent_session_id: Option<String>,
    /// Idempotency key (1 to 256 characters).
    pub idempotency_key: String,
    /// Expected kernel state revision.
    pub expected_state_revision: Option<StateRevision>,
    /// Pinned authorization bundle digest.
    pub policy_bundle_sha: Option<String>,
    /// Deadline (RFC 3339).
    pub deadline_at: Option<Timestamp>,
    /// The operation.
    pub action: Action,
}

impl Principal {
    fn from_value(v: &Value) -> Result<Principal> {
        let m = closed(
            v,
            &["id", "verified_by", "authn_method", "roles"],
            &["id", "verified_by", "authn_method"],
            "principal",
        )?;
        Ok(Principal {
            id: nonempty(m.get("id"), "principal.id")?,
            verified_by: nonempty(m.get("verified_by"), "principal.verified_by")?,
            authn_method: nonempty(m.get("authn_method"), "principal.authn_method")?,
            roles: optional(m.get("roles"), |r| {
                strings(r, 0, true, any, "principal.roles")
            })?
            .unwrap_or_default(),
        })
    }
}

impl Action {
    fn from_value(v: &Value) -> Result<Action> {
        let m = closed(
            v,
            &[
                "kind",
                "resource",
                "action_digest",
                "attributes",
                "requested_capabilities",
            ],
            &["kind", "resource", "action_digest"],
            "action",
        )?;
        let resource = match m.get("resource") {
            Some(r) => {
                let rm = closed(
                    r,
                    &["uri", "canonical_resource_id"],
                    &["uri"],
                    "action.resource",
                )?;
                Resource {
                    uri: nonempty(rm.get("uri"), "action.resource.uri")?,
                    canonical_resource_id: text_or_null(
                        rm.get("canonical_resource_id"),
                        "action.resource.canonical_resource_id",
                    )?,
                }
            }
            None => return violation("action.resource"),
        };
        let attributes = match m.get("attributes") {
            None => None,
            Some(Value::Obj(a)) => Some(a.clone()),
            Some(_) => return violation("action.attributes"),
        };
        Ok(Action {
            kind: matching(m.get("kind"), is_kind, "action.kind")?,
            resource,
            action_digest: text(
                m.get("action_digest"),
                16,
                usize::MAX,
                "action.action_digest",
            )?,
            attributes,
            requested_capabilities: optional(m.get("requested_capabilities"), |r| {
                strings(r, 0, true, any, "action.requested_capabilities")
            })?
            .unwrap_or_default(),
        })
    }
}

impl ActionRequest {
    /// Validate `v` against the ActionRequest schema.
    pub fn from_value(v: &Value) -> Result<ActionRequest> {
        const ALLOWED: [&str; 14] = [
            "contract_version",
            "request_id",
            "trace_id",
            "tenant_id",
            "principal",
            "project_id",
            "session_id",
            "agent_id",
            "parent_session_id",
            "idempotency_key",
            "expected_state_revision",
            "policy_bundle_sha",
            "deadline_at",
            "action",
        ];
        const REQUIRED: [&str; 9] = [
            "contract_version",
            "request_id",
            "trace_id",
            "tenant_id",
            "principal",
            "project_id",
            "session_id",
            "idempotency_key",
            "action",
        ];
        let m = closed(v, &ALLOWED, &REQUIRED, "request")?;
        if m.get("contract_version").and_then(Value::as_str) != Some(CONTRACT_VERSION) {
            return violation("contract_version");
        }
        let expected_state_revision = match m.get("expected_state_revision") {
            None | Some(Value::Null) => None,
            some => Some(StateRevision::read(some, "expected_state_revision")?),
        };
        let deadline_at = match m.get("deadline_at") {
            None | Some(Value::Null) => None,
            Some(Value::Str(t)) => Some(Timestamp::parse_rfc3339(t).ok_or(SchemaError {
                path: "deadline_at",
            })?),
            Some(_) => return violation("deadline_at"),
        };
        Ok(ActionRequest {
            request_id: text(m.get("request_id"), 1, 128, "request_id")?,
            trace_id: text(m.get("trace_id"), 1, 128, "trace_id")?,
            tenant_id: text(m.get("tenant_id"), 1, 128, "tenant_id")?,
            principal: Principal::from_value(m.get("principal").unwrap_or(&Value::Null))?,
            project_id: nonempty(m.get("project_id"), "project_id")?,
            session_id: nonempty(m.get("session_id"), "session_id")?,
            agent_id: text_or_null(m.get("agent_id"), "agent_id")?,
            parent_session_id: text_or_null(m.get("parent_session_id"), "parent_session_id")?,
            idempotency_key: text(m.get("idempotency_key"), 1, 256, "idempotency_key")?,
            expected_state_revision,
            policy_bundle_sha: text_or_null(m.get("policy_bundle_sha"), "policy_bundle_sha")?,
            deadline_at,
            action: Action::from_value(m.get("action").unwrap_or(&Value::Null))?,
        })
    }

    /// Sorted, distinct requested capability set (SR-02).
    pub fn capability_set(&self) -> Vec<String> {
        let mut set = self.action.requested_capabilities.clone();
        set.sort();
        set.dedup();
        set
    }
}

// ===========================================================================
// PolicyBundle (docs/contracts/policy-bundle.schema.json).

/// Statement effect; precedence forbid > require_approval > permit > default deny.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Effect {
    /// Denies; never convertible into an approval or allow.
    Forbid,
    /// Requires a human approval grant.
    RequireApproval,
    /// Permits.
    Permit,
}

/// A policy statement. `None` conditions match anything.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Statement {
    /// Statement id (unique within a loaded bundle).
    pub statement_id: String,
    /// Effect.
    pub effect: Effect,
    /// Tenants the statement applies to.
    pub tenants: Option<Vec<String>>,
    /// Roles of which the principal must hold at least one.
    pub principal_roles: Option<Vec<String>>,
    /// Action kinds and capabilities covered (at least one).
    pub action_kinds: Vec<String>,
    /// Canonical resource id prefixes.
    pub resource_prefixes: Option<Vec<String>>,
    /// Reason code reported when the statement decides.
    pub reason_code: String,
    /// Transition source states (workflow.transition only).
    pub from_states: Option<Vec<String>>,
    /// Transition target states (workflow.transition only).
    pub to_states: Option<Vec<String>>,
}

impl Statement {
    /// Whether the statement covers `workflow.transition`.
    pub fn covers_transition(&self) -> bool {
        self.action_kinds.iter().any(|k| k == WORKFLOW_TRANSITION)
    }

    fn from_value(v: &Value) -> Result<Statement> {
        const ALLOWED: [&str; 9] = [
            "statement_id",
            "effect",
            "tenants",
            "principal_roles",
            "action_kinds",
            "resource_prefixes",
            "reason_code",
            "from_states",
            "to_states",
        ];
        let m = closed(
            v,
            &ALLOWED,
            &["statement_id", "effect", "action_kinds", "reason_code"],
            "statements",
        )?;
        let effect = match m.get("effect").and_then(Value::as_str) {
            Some("forbid") => Effect::Forbid,
            Some("require_approval") => Effect::RequireApproval,
            Some("permit") => Effect::Permit,
            _ => return violation("statements.effect"),
        };
        Ok(Statement {
            statement_id: nonempty(m.get("statement_id"), "statements.statement_id")?,
            effect,
            tenants: optional(m.get("tenants"), |x| {
                strings(x, 0, true, not_empty, "statements.tenants")
            })?,
            principal_roles: optional(m.get("principal_roles"), |x| {
                strings(x, 0, true, not_empty, "statements.principal_roles")
            })?,
            action_kinds: strings(
                m.get("action_kinds"),
                1,
                true,
                not_empty,
                "statements.action_kinds",
            )?,
            resource_prefixes: optional(m.get("resource_prefixes"), |x| {
                strings(x, 0, true, not_empty, "statements.resource_prefixes")
            })?,
            reason_code: matching(
                m.get("reason_code"),
                is_reason_code,
                "statements.reason_code",
            )?,
            from_states: optional(m.get("from_states"), |x| {
                strings(x, 1, true, is_name, "statements.from_states")
            })?,
            to_states: optional(m.get("to_states"), |x| {
                strings(x, 1, true, is_name, "statements.to_states")
            })?,
        })
    }
}

/// A protected workflow edge.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Edge {
    /// Source state.
    pub from_state: String,
    /// Target state.
    pub to_state: String,
}

/// The authoritative transition protection classification carried by an
/// authorization bundle (D-019 N-1, N-4). Schema shape only; duplicates, self
/// edges and bypassable edges are rejected at bundle load ([`crate::policy`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkflowProtection {
    /// Acceptance-policy bundle this authorization bundle is compatible with.
    pub acceptance_policy_bundle_sha: String,
    /// States protected from any source.
    pub protected_to_states: Vec<String>,
    /// Source-specific protected edges.
    pub protected_transitions: Vec<Edge>,
}

impl WorkflowProtection {
    fn from_value(v: &Value) -> Result<WorkflowProtection> {
        let keys = [
            "acceptance_policy_bundle_sha",
            "protected_to_states",
            "protected_transitions",
        ];
        let m = exact(v, &keys, "workflow_protection")?;
        let Some(Value::Arr(edges)) = m.get("protected_transitions") else {
            return violation("workflow_protection.protected_transitions");
        };
        let protected_transitions = edges
            .iter()
            .map(|e| {
                let em = closed(
                    e,
                    &["from_state", "to_state"],
                    &["from_state", "to_state"],
                    "workflow_protection.protected_transitions",
                )?;
                Ok(Edge {
                    from_state: matching(
                        em.get("from_state"),
                        is_name,
                        "workflow_protection.protected_transitions.from_state",
                    )?,
                    to_state: matching(
                        em.get("to_state"),
                        is_name,
                        "workflow_protection.protected_transitions.to_state",
                    )?,
                })
            })
            .collect::<Result<Vec<Edge>>>()?;
        Ok(WorkflowProtection {
            acceptance_policy_bundle_sha: matching(
                m.get("acceptance_policy_bundle_sha"),
                is_digest,
                "workflow_protection.acceptance_policy_bundle_sha",
            )?,
            protected_to_states: strings(
                m.get("protected_to_states"),
                0,
                false,
                is_name,
                "workflow_protection.protected_to_states",
            )?,
            protected_transitions,
        })
    }

    /// Union rule (D-019 N-4): protected when the target is a protected state
    /// (from any source) or the exact edge is a protected edge.
    pub fn is_protected(&self, from_state: &str, to_state: &str) -> bool {
        self.protected_to_states.iter().any(|s| s == to_state)
            || self
                .protected_transitions
                .iter()
                .any(|e| e.from_state == from_state && e.to_state == to_state)
    }
}

/// A PolicyBundle, schema-valid by construction (load-time consistency rules
/// are applied by [`crate::policy::load_bundle`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PolicyBundle {
    /// Bundle id.
    pub bundle_id: String,
    /// Bundle version.
    pub version: String,
    /// Publisher.
    pub publisher: String,
    /// Description.
    pub description: Option<String>,
    /// Capabilities the bundle knows (unique, at least one).
    pub known_capabilities: Vec<String>,
    /// Issuers whose verified principals are trusted (unique, at least one).
    pub trusted_principal_issuers: Vec<String>,
    /// Statements.
    pub statements: Vec<Statement>,
    /// Publisher signature (not verified by the key-free kernel; not part of the digest).
    pub publisher_signature: Option<String>,
    /// Transition protection classification.
    pub workflow_protection: Option<WorkflowProtection>,
}

impl PolicyBundle {
    /// Validate `v` against the PolicyBundle schema.
    pub fn from_value(v: &Value) -> Result<PolicyBundle> {
        const ALLOWED: [&str; 10] = [
            "contract_version",
            "bundle_id",
            "version",
            "publisher",
            "description",
            "known_capabilities",
            "trusted_principal_issuers",
            "statements",
            "publisher_signature",
            "workflow_protection",
        ];
        const REQUIRED: [&str; 7] = [
            "contract_version",
            "bundle_id",
            "version",
            "publisher",
            "known_capabilities",
            "trusted_principal_issuers",
            "statements",
        ];
        let m = closed(v, &ALLOWED, &REQUIRED, "bundle")?;
        if m.get("contract_version").and_then(Value::as_str) != Some(CONTRACT_VERSION) {
            return violation("contract_version");
        }
        let Some(Value::Arr(statements)) = m.get("statements") else {
            return violation("statements");
        };
        Ok(PolicyBundle {
            bundle_id: nonempty(m.get("bundle_id"), "bundle_id")?,
            version: nonempty(m.get("version"), "version")?,
            publisher: nonempty(m.get("publisher"), "publisher")?,
            description: text_or_null(m.get("description"), "description")?,
            known_capabilities: strings(
                m.get("known_capabilities"),
                1,
                true,
                is_kind,
                "known_capabilities",
            )?,
            trusted_principal_issuers: strings(
                m.get("trusted_principal_issuers"),
                1,
                true,
                not_empty,
                "trusted_principal_issuers",
            )?,
            statements: statements
                .iter()
                .map(Statement::from_value)
                .collect::<Result<Vec<_>>>()?,
            publisher_signature: text_or_null(m.get("publisher_signature"), "publisher_signature")?,
            workflow_protection: match m.get("workflow_protection") {
                None => None,
                Some(p) => Some(WorkflowProtection::from_value(p)?),
            },
        })
    }
}

// ===========================================================================
// Decision (docs/contracts/decision.schema.json).

/// Decision status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecisionStatus {
    /// Permitted.
    Allow,
    /// Denied by policy or by a structural authorization rule.
    Deny,
    /// Needs a human approval grant.
    RequireApproval,
    /// Policy missing, rejected or pinned to another version: fail closed.
    Unavailable,
    /// The request cannot be decided as given.
    Invalid,
}

impl DecisionStatus {
    /// Contract spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            DecisionStatus::Allow => "allow",
            DecisionStatus::Deny => "deny",
            DecisionStatus::RequireApproval => "require_approval",
            DecisionStatus::Unavailable => "unavailable",
            DecisionStatus::Invalid => "invalid",
        }
    }
}

/// Reason codes produced by the kernel itself (internal codes; agents see
/// the [`crate::refusal`] mapping).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KernelCode {
    /// Decide input is not strict JSON (D-021).
    RequestJsonInvalid,
    /// Request (or decide input) violates its schema (D-022).
    RequestSchemaInvalid,
    /// `requested_capabilities` repeats `action.kind` (D-018 A4).
    RequestedCapabilitiesInvalid,
    /// Deadline at or before `now`.
    DeadlineExceeded,
    /// No policy bundle.
    PolicyUnavailable,
    /// Bundle rejected at load.
    PolicyBundleRejected,
    /// Pinned bundle digest differs from the loaded one.
    PolicyVersionMismatch,
    /// Principal issuer not trusted by the bundle.
    PrincipalUnverified,
    /// Expected state revision differs.
    StateRevisionConflict,
    /// A capability the bundle does not know.
    UnknownCapability,
    /// Resource id missing or not canonical.
    ResourceNotCanonical,
    /// Resource outside the request's tenant and project.
    ResourceScopeMismatch,
    /// Transition known but not supported by the bundle (D-020).
    UnsupportedCapability,
    /// Transition resource is not `workflow:<tenant>/<project>/<workflow_id>`.
    WorkflowResourceInvalid,
    /// Transition attributes malformed.
    TransitionMalformed,
    /// Transition combined with other capabilities.
    CompositeNotSupported,
    /// Protection metadata unavailable (fail closed).
    ProtectionUnavailable,
    /// Protected transition without the acceptance binding.
    AcceptanceBindingRequired,
    /// Unprotected transition with an acceptance binding.
    AcceptanceBindingUnexpected,
    /// No statement authorizes the request.
    DefaultDeny,
    /// A requested capability is covered by no matching approval or permit.
    CapabilityNotPermitted,
}

impl KernelCode {
    /// Contract spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            KernelCode::RequestJsonInvalid => "REQUEST_JSON_INVALID",
            KernelCode::RequestSchemaInvalid => "REQUEST_SCHEMA_INVALID",
            KernelCode::RequestedCapabilitiesInvalid => "REQUESTED_CAPABILITIES_INVALID",
            KernelCode::DeadlineExceeded => "DEADLINE_EXCEEDED",
            KernelCode::PolicyUnavailable => "POLICY_UNAVAILABLE",
            KernelCode::PolicyBundleRejected => "POLICY_BUNDLE_REJECTED",
            KernelCode::PolicyVersionMismatch => "POLICY_VERSION_MISMATCH",
            KernelCode::PrincipalUnverified => "PRINCIPAL_UNVERIFIED",
            KernelCode::StateRevisionConflict => "STATE_REVISION_CONFLICT",
            KernelCode::UnknownCapability => "UNKNOWN_CAPABILITY",
            KernelCode::ResourceNotCanonical => "RESOURCE_NOT_CANONICAL",
            KernelCode::ResourceScopeMismatch => "RESOURCE_SCOPE_MISMATCH",
            KernelCode::UnsupportedCapability => "UNSUPPORTED_CAPABILITY",
            KernelCode::WorkflowResourceInvalid => "WORKFLOW_RESOURCE_INVALID",
            KernelCode::TransitionMalformed => "TRANSITION_MALFORMED",
            KernelCode::CompositeNotSupported => "COMPOSITE_NOT_SUPPORTED",
            KernelCode::ProtectionUnavailable => "PROTECTION_UNAVAILABLE",
            KernelCode::AcceptanceBindingRequired => "ACCEPTANCE_BINDING_REQUIRED",
            KernelCode::AcceptanceBindingUnexpected => "ACCEPTANCE_BINDING_UNEXPECTED",
            KernelCode::DefaultDeny => "DEFAULT_DENY",
            KernelCode::CapabilityNotPermitted => "CAPABILITY_NOT_PERMITTED",
        }
    }
}

/// The reason of a decision: a kernel code or the deciding statement's code.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReasonCode {
    /// Produced by the kernel.
    Kernel(KernelCode),
    /// Taken from the deciding policy statement.
    Policy(String),
}

impl ReasonCode {
    /// Contract spelling.
    pub fn as_str(&self) -> &str {
        match self {
            ReasonCode::Kernel(k) => k.as_str(),
            ReasonCode::Policy(s) => s,
        }
    }
}

/// `policy_bundle_sha` of a decision made without a loaded bundle.
pub const NO_POLICY_SHA: &str = "none";

/// A Decision contract instance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    /// Request id (`unknown` when the request has none).
    pub request_id: String,
    /// Status.
    pub status: DecisionStatus,
    /// Loaded bundle digest, or [`NO_POLICY_SHA`].
    pub policy_bundle_sha: String,
    /// Reason.
    pub reason_code: ReasonCode,
    /// Matched statement ids, sorted by byte order, unique.
    pub matched_rule_ids: Vec<String>,
    /// Kernel state revision the decision was made against.
    pub state_revision: StateRevision,
    /// Trusted decision time (input).
    pub decided_at: Timestamp,
    /// Deterministic approval request id for `require_approval`.
    pub approval_request_reference: Option<String>,
    /// Expiry of an `allow` or `require_approval` decision.
    pub expires_at: Option<Timestamp>,
}

/// `<prefix>:` plus the first 32 hex characters of the D-006 digest of `binding`.
///
/// Callers build `binding` from strings and validated revisions only, so it is
/// inside the D-006 domain and its compact text is its canonical text.
pub(crate) fn short_id(prefix: &str, binding: &Value) -> String {
    let d = Digest::of_bytes(binding.to_json_text().as_bytes());
    format!("{prefix}:{}", d.hex().get(..32).unwrap_or(""))
}

impl Decision {
    /// Deterministic decision id: binds request, policy, revision, status,
    /// reason and time.
    pub fn decision_id(&self) -> String {
        let binding = json::obj([
            ("request", json::s(&self.request_id)),
            ("policy", json::s(&self.policy_bundle_sha)),
            ("revision", Value::Int(self.state_revision.get())),
            ("status", json::s(self.status.as_str())),
            ("reason", json::s(self.reason_code.as_str())),
            ("at", json::s(&self.decided_at.to_string())),
        ]);
        short_id("dec", &binding)
    }

    /// The Decision contract JSON.
    pub fn to_value(&self) -> Value {
        let time_or_null =
            |t: Option<Timestamp>| t.map_or(Value::Null, |t| json::s(&t.to_string()));
        json::obj([
            ("contract_version", json::s(CONTRACT_VERSION)),
            ("decision_id", json::s(&self.decision_id())),
            ("request_id", json::s(&self.request_id)),
            ("status", json::s(self.status.as_str())),
            ("policy_bundle_sha", json::s(&self.policy_bundle_sha)),
            ("reason_code", json::s(self.reason_code.as_str())),
            ("safe_message", Value::Null),
            (
                "matched_rule_ids",
                Value::Arr(self.matched_rule_ids.iter().map(|x| json::s(x)).collect()),
            ),
            ("state_revision", Value::Int(self.state_revision.get())),
            ("decided_at", json::s(&self.decided_at.to_string())),
            ("permit_reference", Value::Null),
            (
                "approval_request_reference",
                self.approval_request_reference
                    .as_deref()
                    .map_or(Value::Null, json::s),
            ),
            ("expires_at", time_or_null(self.expires_at)),
        ])
    }
}

// ===========================================================================
// Acceptance contracts (m1/fixtures/acceptance/SEMANTICS.md, m1-draft-3).

/// A workflow transition `from_state` to `to_state` (state names).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Transition {
    /// Source state.
    pub from_state: String,
    /// Target state.
    pub to_state: String,
}

impl Transition {
    fn read(v: Option<&Value>, path: &'static str) -> Result<Transition> {
        let m = exact(v.unwrap_or(&Value::Null), &["from_state", "to_state"], path)?;
        Ok(Transition {
            from_state: matching(m.get("from_state"), is_name, path)?,
            to_state: matching(m.get("to_state"), is_name, path)?,
        })
    }
}

/// The acceptance policy a record, requirement set or broker state is bound to
/// (distinct from the authorization bundle, D-018 A3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptancePolicyRef {
    /// Acceptance policy id.
    pub acceptance_policy_id: String,
    /// Acceptance policy version (`^[0-9]{1,9}$`).
    pub acceptance_policy_version: String,
    /// Acceptance-policy bundle digest.
    pub acceptance_policy_bundle_sha: String,
}

impl AcceptancePolicyRef {
    fn read(m: &Map, path: &'static str) -> Result<AcceptancePolicyRef> {
        Ok(AcceptancePolicyRef {
            acceptance_policy_id: matching(m.get("acceptance_policy_id"), is_id, path)?,
            acceptance_policy_version: matching(
                m.get("acceptance_policy_version"),
                is_policy_version,
                path,
            )?,
            acceptance_policy_bundle_sha: matching(
                m.get("acceptance_policy_bundle_sha"),
                is_digest,
                path,
            )?,
        })
    }
}

/// Criterion class.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CriterionClass {
    /// Mechanical check (trust M).
    Mechanical,
    /// Evidence check (trust E).
    Evidence,
    /// Human judgment (trust H).
    Human,
    /// Model-assisted: advisory only, never mandatory (D-017).
    ModelAssisted,
}

impl CriterionClass {
    fn read(
        v: Option<&Value>,
        allow_model_assisted: bool,
        path: &'static str,
    ) -> Result<CriterionClass> {
        match v.and_then(Value::as_str) {
            Some("mechanical") => Ok(CriterionClass::Mechanical),
            Some("evidence") => Ok(CriterionClass::Evidence),
            Some("human") => Ok(CriterionClass::Human),
            Some("model_assisted") if allow_model_assisted => Ok(CriterionClass::ModelAssisted),
            _ => violation(path),
        }
    }

    /// The only evaluator trust that satisfies a required criterion of this class.
    pub fn required_trust(self) -> Option<EvaluatorTrust> {
        match self {
            CriterionClass::Mechanical => Some(EvaluatorTrust::Mechanical),
            CriterionClass::Evidence => Some(EvaluatorTrust::Evidence),
            CriterionClass::Human => Some(EvaluatorTrust::Human),
            CriterionClass::ModelAssisted => None,
        }
    }
}

/// Criterion status in a record.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CriterionStatus {
    /// Passed.
    Pass,
    /// Failed.
    Fail,
    /// Not determined.
    Unknown,
    /// Result no longer current.
    Stale,
    /// Waived.
    Waived,
}

/// Evaluator trust label: a claim inside an untrusted record, never proof of
/// evaluator identity or registration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvaluatorTrust {
    /// `U`: untrusted.
    Untrusted,
    /// `MA`: model-assisted.
    ModelAssisted,
    /// `M`: mechanical.
    Mechanical,
    /// `E`: evidence.
    Evidence,
    /// `H`: human.
    Human,
}

/// Record status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecordStatus {
    /// Accepted.
    Accepted,
    /// Rejected.
    Rejected,
    /// Incomplete.
    Incomplete,
    /// Needs human review.
    NeedsHumanReview,
}

/// One criterion of an acceptance record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecordCriterion {
    /// Criterion id.
    pub criterion_id: String,
    /// Whether the record claims the criterion is mandatory.
    pub mandatory: bool,
    /// Class.
    pub required_class: CriterionClass,
    /// Status.
    pub status: CriterionStatus,
    /// Evaluator id.
    pub evaluator_id: String,
    /// Claimed evaluator trust.
    pub evaluator_trust: EvaluatorTrust,
    /// End of the result's freshness.
    pub fresh_until: Timestamp,
}

impl RecordCriterion {
    fn from_value(v: &Value) -> Result<RecordCriterion> {
        const KEYS: [&str; 7] = [
            "criterion_id",
            "mandatory",
            "required_class",
            "status",
            "evaluator_id",
            "evaluator_trust",
            "fresh_until",
        ];
        let path = "record.criteria";
        let m = exact(v, &KEYS, path)?;
        let mandatory = m
            .get("mandatory")
            .and_then(Value::as_bool)
            .ok_or(SchemaError { path })?;
        let required_class = CriterionClass::read(m.get("required_class"), true, path)?;
        // A model-assisted criterion is advisory only (D-017).
        if mandatory && required_class == CriterionClass::ModelAssisted {
            return violation(path);
        }
        let status = match m.get("status").and_then(Value::as_str) {
            Some("pass") => CriterionStatus::Pass,
            Some("fail") => CriterionStatus::Fail,
            Some("unknown") => CriterionStatus::Unknown,
            Some("stale") => CriterionStatus::Stale,
            Some("waived") => CriterionStatus::Waived,
            _ => return violation(path),
        };
        let evaluator_trust = match m.get("evaluator_trust").and_then(Value::as_str) {
            Some("U") => EvaluatorTrust::Untrusted,
            Some("MA") => EvaluatorTrust::ModelAssisted,
            Some("M") => EvaluatorTrust::Mechanical,
            Some("E") => EvaluatorTrust::Evidence,
            Some("H") => EvaluatorTrust::Human,
            _ => return violation(path),
        };
        Ok(RecordCriterion {
            criterion_id: matching(m.get("criterion_id"), is_name, path)?,
            mandatory,
            required_class,
            status,
            evaluator_id: matching(m.get("evaluator_id"), is_name, path)?,
            evaluator_trust,
            fresh_until: strict_time(m.get("fresh_until"), path)?,
        })
    }
}

fn unique_criteria<'a>(ids: impl Iterator<Item = &'a str>) -> bool {
    let mut seen: Vec<&str> = Vec::new();
    for id in ids {
        if seen.contains(&id) {
            return false;
        }
        seen.push(id);
    }
    true
}

/// An acceptance record as presented with a promotion request (untrusted).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptanceRecord {
    /// Record id.
    pub acceptance_record_id: String,
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Task.
    pub task_id: String,
    /// Workflow.
    pub workflow_id: String,
    /// Transition the record accepts.
    pub transition: Transition,
    /// Artifact snapshot id.
    pub artifact_snapshot_id: String,
    /// Artifact digest.
    pub artifact_digest: String,
    /// Acceptance policy.
    pub policy: AcceptancePolicyRef,
    /// Workflow revision the record was issued for.
    pub state_revision: StateRevision,
    /// Claimed overall status.
    pub status: RecordStatus,
    /// Issue time.
    pub issued_at: Timestamp,
    /// Expiry time.
    pub expires_at: Timestamp,
    /// Criteria (non-empty, unique ids).
    pub criteria: Vec<RecordCriterion>,
    /// Claimed `record_digest`.
    pub record_digest: String,
    /// D-006 digest of the record without `record_digest`, computed at construction.
    pub computed_digest: Digest,
}

impl AcceptanceRecord {
    /// Validate `v` against the acceptance record contract.
    pub fn from_value(v: &Value) -> Result<AcceptanceRecord> {
        const KEYS: [&str; 18] = [
            "contract_version",
            "acceptance_record_id",
            "tenant_id",
            "project_id",
            "task_id",
            "workflow_id",
            "transition",
            "artifact_snapshot_id",
            "artifact_digest",
            "acceptance_policy_id",
            "acceptance_policy_version",
            "acceptance_policy_bundle_sha",
            "state_revision",
            "status",
            "issued_at",
            "expires_at",
            "criteria",
            "record_digest",
        ];
        let path = "record";
        let m = exact(v, &KEYS, path)?;
        if m.get("contract_version").and_then(Value::as_str) != Some(ACCEPTANCE_CONTRACT_VERSION) {
            return violation("record.contract_version");
        }
        let status = match m.get("status").and_then(Value::as_str) {
            Some("accepted") => RecordStatus::Accepted,
            Some("rejected") => RecordStatus::Rejected,
            Some("incomplete") => RecordStatus::Incomplete,
            Some("needs_human_review") => RecordStatus::NeedsHumanReview,
            _ => return violation("record.status"),
        };
        let criteria = match m.get("criteria") {
            Some(Value::Arr(items)) if !items.is_empty() => items
                .iter()
                .map(RecordCriterion::from_value)
                .collect::<Result<Vec<_>>>()?,
            _ => return violation("record.criteria"),
        };
        if !unique_criteria(criteria.iter().map(|c| c.criterion_id.as_str())) {
            return violation("record.criteria");
        }
        let computed_digest =
            json::digest(&v.without("record_digest")).map_err(|_| SchemaError { path })?;
        Ok(AcceptanceRecord {
            acceptance_record_id: matching(m.get("acceptance_record_id"), is_id, path)?,
            tenant_id: matching(m.get("tenant_id"), is_id, path)?,
            project_id: matching(m.get("project_id"), is_id, path)?,
            task_id: matching(m.get("task_id"), is_id, path)?,
            workflow_id: matching(m.get("workflow_id"), is_name, path)?,
            transition: Transition::read(m.get("transition"), "record.transition")?,
            artifact_snapshot_id: matching(m.get("artifact_snapshot_id"), is_id, path)?,
            artifact_digest: matching(m.get("artifact_digest"), is_digest, path)?,
            policy: AcceptancePolicyRef::read(m, path)?,
            state_revision: StateRevision::read(m.get("state_revision"), path)?,
            status,
            issued_at: strict_time(m.get("issued_at"), path)?,
            expires_at: strict_time(m.get("expires_at"), path)?,
            criteria,
            record_digest: matching(m.get("record_digest"), is_digest, path)?,
            computed_digest,
        })
    }
}

/// Trusted broker state for an acceptance check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CurrentState {
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Task.
    pub task_id: String,
    /// Workflow.
    pub workflow_id: String,
    /// Requested transition.
    pub transition: Transition,
    /// Current artifact snapshot id.
    pub artifact_snapshot_id: String,
    /// Current artifact digest.
    pub artifact_digest: String,
    /// Active acceptance policy.
    pub policy: AcceptancePolicyRef,
    /// Current workflow revision.
    pub state_revision: StateRevision,
    /// Trusted, injected time.
    pub now: Timestamp,
    /// Record ids already consumed.
    pub consumed_record_ids: Vec<String>,
    /// Revoked evaluator ids.
    pub revoked_evaluator_ids: Vec<String>,
}

impl CurrentState {
    /// Validate `v` against the broker state contract.
    pub fn from_value(v: &Value) -> Result<CurrentState> {
        const KEYS: [&str; 14] = [
            "tenant_id",
            "project_id",
            "task_id",
            "workflow_id",
            "transition",
            "artifact_snapshot_id",
            "artifact_digest",
            "acceptance_policy_id",
            "acceptance_policy_version",
            "acceptance_policy_bundle_sha",
            "state_revision",
            "now",
            "consumed_record_ids",
            "revoked_evaluator_ids",
        ];
        let path = "current";
        let m = exact(v, &KEYS, path)?;
        Ok(CurrentState {
            tenant_id: matching(m.get("tenant_id"), is_id, path)?,
            project_id: matching(m.get("project_id"), is_id, path)?,
            task_id: matching(m.get("task_id"), is_id, path)?,
            workflow_id: matching(m.get("workflow_id"), is_name, path)?,
            transition: Transition::read(m.get("transition"), "current.transition")?,
            artifact_snapshot_id: matching(m.get("artifact_snapshot_id"), is_id, path)?,
            artifact_digest: matching(m.get("artifact_digest"), is_digest, path)?,
            policy: AcceptancePolicyRef::read(m, path)?,
            state_revision: StateRevision::read(m.get("state_revision"), path)?,
            now: strict_time(m.get("now"), path)?,
            consumed_record_ids: strings(
                m.get("consumed_record_ids"),
                0,
                false,
                is_id,
                "current.consumed_record_ids",
            )?,
            revoked_evaluator_ids: strings(
                m.get("revoked_evaluator_ids"),
                0,
                false,
                is_name,
                "current.revoked_evaluator_ids",
            )?,
        })
    }
}

/// One required criterion of a trusted requirement set (always mandatory).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequiredCriterion {
    /// Criterion id.
    pub criterion_id: String,
    /// Required class (never model-assisted).
    pub required_class: CriterionClass,
}

/// The trusted, version-bound requirement set of the active acceptance policy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptanceRequirements {
    /// Acceptance policy the set belongs to.
    pub policy: AcceptancePolicyRef,
    /// Required criteria (non-empty, unique ids).
    pub criteria: Vec<RequiredCriterion>,
}

impl AcceptanceRequirements {
    /// Validate `v` against the requirement set contract.
    pub fn from_value(v: &Value) -> Result<AcceptanceRequirements> {
        let path = "requirements";
        let m = exact(
            v,
            &[
                "acceptance_policy_id",
                "acceptance_policy_version",
                "acceptance_policy_bundle_sha",
                "criteria",
            ],
            path,
        )?;
        let policy = AcceptancePolicyRef::read(m, path)?;
        let criteria = match m.get("criteria") {
            Some(Value::Arr(items)) if !items.is_empty() => items
                .iter()
                .map(|c| {
                    let cm = exact(
                        c,
                        &["criterion_id", "required_class"],
                        "requirements.criteria",
                    )?;
                    Ok(RequiredCriterion {
                        criterion_id: matching(
                            cm.get("criterion_id"),
                            is_name,
                            "requirements.criteria",
                        )?,
                        required_class: CriterionClass::read(
                            cm.get("required_class"),
                            false,
                            "requirements.criteria",
                        )?,
                    })
                })
                .collect::<Result<Vec<_>>>()?,
            _ => return violation("requirements.criteria"),
        };
        if !unique_criteria(criteria.iter().map(|c| c.criterion_id.as_str())) {
            return violation("requirements.criteria");
        }
        Ok(AcceptanceRequirements { policy, criteria })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::json::parse;

    type Edit = Box<dyn FnOnce(&mut Map)>;

    const REQUEST: &str = r#"{"contract_version":"2.0","request_id":"r1","trace_id":"t1","tenant_id":"acme","principal":{"id":"agent:1","verified_by":"adapter","authn_method":"peer","roles":["agent"]},"project_id":"web","session_id":"s1","idempotency_key":"k1","action":{"kind":"file.write","resource":{"uri":"file:///x","canonical_resource_id":"workspace:acme/web/x"},"action_digest":"sha256:0123456789abcdef"}}"#;

    fn request_with(
        edit: impl FnOnce(&mut Map),
    ) -> std::result::Result<ActionRequest, SchemaError> {
        let mut v = parse(REQUEST.as_bytes()).unwrap();
        if let Value::Obj(m) = &mut v {
            edit(m);
        }
        ActionRequest::from_value(&v)
    }

    #[test]
    fn request_schema_accepts_the_base_and_binds_tenant_and_project() {
        let r = request_with(|_| {}).unwrap();
        assert_eq!(
            (r.tenant_id.as_str(), r.project_id.as_str()),
            ("acme", "web")
        );
        assert_eq!(r.principal.roles, vec!["agent".to_string()]);
        assert!(r.action.requested_capabilities.is_empty() && r.action.attributes.is_none());
    }

    #[test]
    fn request_schema_rejections() {
        let cases: Vec<(&str, Edit)> = vec![
            (
                "contract_version",
                Box::new(|m: &mut Map| {
                    m.insert("contract_version".into(), json::s("1.0"));
                }),
            ),
            (
                "extra member",
                Box::new(|m: &mut Map| {
                    m.insert("extra".into(), Value::Null);
                }),
            ),
            (
                "missing tenant",
                Box::new(|m: &mut Map| {
                    m.remove("tenant_id");
                }),
            ),
            (
                "empty tenant",
                Box::new(|m: &mut Map| {
                    m.insert("tenant_id".into(), json::s(""));
                }),
            ),
            (
                "long tenant",
                Box::new(|m: &mut Map| {
                    m.insert("tenant_id".into(), json::s(&"t".repeat(129)));
                }),
            ),
            (
                "negative revision",
                Box::new(|m: &mut Map| {
                    m.insert("expected_state_revision".into(), Value::Int(-1));
                }),
            ),
            (
                "bad deadline",
                Box::new(|m: &mut Map| {
                    m.insert("deadline_at".into(), json::s("tomorrow"));
                }),
            ),
            (
                "numeric agent",
                Box::new(|m: &mut Map| {
                    m.insert("agent_id".into(), Value::Int(1));
                }),
            ),
            (
                "long key",
                Box::new(|m: &mut Map| {
                    m.insert("idempotency_key".into(), json::s(&"k".repeat(257)));
                }),
            ),
        ];
        for (name, edit) in cases {
            assert!(request_with(edit).is_err(), "{name}");
        }
        let nested = [
            r#""principal":{"id":"a","verified_by":"b","authn_method":"c","roles":["x","x"]}"#,
            r#""principal":{"id":"a","verified_by":"b","authn_method":"c","extra":1}"#,
            r#""principal":{"id":"a","verified_by":"","authn_method":"c"}"#,
        ];
        for p in nested {
            let text = REQUEST.replace(r#""principal":{"id":"agent:1","verified_by":"adapter","authn_method":"peer","roles":["agent"]}"#, p);
            assert!(
                ActionRequest::from_value(&parse(text.as_bytes()).unwrap()).is_err(),
                "{p}"
            );
        }
        for (old, new) in [
            (r#""kind":"file.write""#, r#""kind":"File.write""#),
            (
                r#""action_digest":"sha256:0123456789abcdef""#,
                r#""action_digest":"short""#,
            ),
            (r#""uri":"file:///x""#, r#""uri":"""#),
            (
                r#""canonical_resource_id":"workspace:acme/web/x""#,
                r#""canonical_resource_id":7"#,
            ),
            (
                r#""action_digest":"sha256:0123456789abcdef""#,
                r#""action_digest":"sha256:0123456789abcdef","attributes":[]"#,
            ),
            (
                r#""action_digest":"sha256:0123456789abcdef""#,
                r#""action_digest":"sha256:0123456789abcdef","requested_capabilities":["a","a"]"#,
            ),
            (
                r#""action_digest":"sha256:0123456789abcdef""#,
                r#""action_digest":"sha256:0123456789abcdef","other":1"#,
            ),
        ] {
            let text = REQUEST.replacen(old, new, 1);
            assert!(
                ActionRequest::from_value(&parse(text.as_bytes()).unwrap()).is_err(),
                "{new}"
            );
        }
        assert!(ActionRequest::from_value(&Value::Arr(vec![])).is_err());
    }

    #[test]
    fn request_optional_members() {
        let r = request_with(|m| {
            m.insert(
                "expected_state_revision".into(),
                Value::Int(MAX_SAFE_INTEGER),
            );
            m.insert("deadline_at".into(), json::s("2026-11-07T12:00:00+01:00"));
            m.insert("policy_bundle_sha".into(), Value::Null);
            m.insert("agent_id".into(), Value::Null);
        })
        .unwrap();
        assert_eq!(
            r.expected_state_revision.map(StateRevision::get),
            Some(MAX_SAFE_INTEGER)
        );
        assert_eq!(
            r.deadline_at.map(|t| t.to_string()).as_deref(),
            Some("2026-11-07T11:00:00Z")
        );
        assert_eq!(r.policy_bundle_sha, None);
    }

    #[test]
    fn state_revision_range() {
        assert!(StateRevision::new(0).is_some() && StateRevision::new(MAX_SAFE_INTEGER).is_some());
        assert!(
            StateRevision::new(-1).is_none() && StateRevision::new(MAX_SAFE_INTEGER + 1).is_none()
        );
    }

    #[test]
    fn decision_contract_shape() {
        let d = Decision {
            request_id: "r1".into(),
            status: DecisionStatus::Allow,
            policy_bundle_sha: NO_POLICY_SHA.into(),
            reason_code: ReasonCode::Policy("OK".into()),
            matched_rule_ids: vec!["a".into()],
            state_revision: StateRevision::new(3).unwrap(),
            decided_at: Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap(),
            approval_request_reference: None,
            expires_at: None,
        };
        let v = d.to_value();
        let m = v.as_obj().unwrap();
        assert_eq!(m.len(), 13);
        assert!(d.decision_id().starts_with("dec:") && d.decision_id().len() == 36);
        assert_eq!(v.get("status").and_then(Value::as_str), Some("allow"));
        assert_eq!(v.get("expires_at"), Some(&Value::Null));
        let other = Decision {
            status: DecisionStatus::Deny,
            ..d.clone()
        };
        assert_ne!(other.decision_id(), d.decision_id());
    }

    #[test]
    fn protection_union_rule() {
        let p = WorkflowProtection {
            acceptance_policy_bundle_sha: format!("sha256:{}", "c".repeat(64)),
            protected_to_states: vec!["accepted".into()],
            protected_transitions: vec![Edge {
                from_state: "draft".into(),
                to_state: "verifying".into(),
            }],
        };
        assert!(p.is_protected("anything", "accepted"));
        assert!(p.is_protected("draft", "verifying"));
        assert!(!p.is_protected("planning", "verifying"));
    }

    #[test]
    fn requirements_reject_model_assisted_and_duplicates() {
        let base = format!(
            r#"{{"acceptance_policy_id":"p","acceptance_policy_version":"1","acceptance_policy_bundle_sha":"sha256:{}","criteria":CRIT}}"#,
            "c".repeat(64)
        );
        let ok = base.replace(
            "CRIT",
            r#"[{"criterion_id":"tests","required_class":"mechanical"}]"#,
        );
        assert!(AcceptanceRequirements::from_value(&parse(ok.as_bytes()).unwrap()).is_ok());
        for crit in [
            r#"[]"#,
            r#"[{"criterion_id":"tests","required_class":"model_assisted"}]"#,
            r#"[{"criterion_id":"tests","required_class":"mechanical"},{"criterion_id":"tests","required_class":"human"}]"#,
            r#"[{"criterion_id":"Tests","required_class":"mechanical"}]"#,
            r#"[{"criterion_id":"tests","required_class":"mechanical","mandatory":true}]"#,
        ] {
            let text = base.replace("CRIT", crit);
            assert!(
                AcceptanceRequirements::from_value(&parse(text.as_bytes()).unwrap()).is_err(),
                "{crit}"
            );
        }
    }
}
