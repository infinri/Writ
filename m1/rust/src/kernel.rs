//! Kernel: pure authorization decision (D-008 extended by D-017..D-019).
//!
//! No I/O, no clock, no keys, no model SDK. Every input, including `now`, is
//! an argument; identical inputs give identical decisions.

use crate::ids::{is_digest, is_kind, is_name, is_reason_code};
use crate::json::{self, obj, s, Value};
use crate::time::{self, Instant};
use std::collections::{BTreeSet, HashSet, VecDeque};

pub const TRANSITION: &str = "workflow.transition";
const DECISION_TTL_SECONDS: i64 = 300;
pub const ACCEPTANCE_ATTRS: [&str; 3] = ["acceptance_record_id", "artifact_snapshot_id", "artifact_digest"];

// ---------------------------------------------------------------------------
// Minimal schema helpers (hand-coded from docs/contracts/*.schema.json).

fn chars(v: &str) -> usize {
    v.chars().count()
}

fn str_len(v: Option<&Value>, min: usize, max: usize) -> bool {
    matches!(v, Some(Value::Str(x)) if (min..=max).contains(&chars(x)))
}

fn opt_str_or_null(v: Option<&Value>) -> bool {
    matches!(v, None | Some(Value::Null) | Some(Value::Str(_)))
}

fn unique(items: &[Value]) -> bool {
    items.iter().enumerate().all(|(i, a)| items[..i].iter().all(|b| a != b))
}

fn only_keys(v: &Value, allowed: &[&str]) -> bool {
    v.as_obj().is_some_and(|m| m.iter().all(|(k, _)| allowed.contains(&k.as_str())))
}

fn has_keys(v: &Value, required: &[&str]) -> bool {
    required.iter().all(|k| v.get(k).is_some())
}

/// Array of strings with optional per-item check, uniqueness and minimum size.
fn str_array(v: Option<&Value>, min_items: usize, uniq: bool, item: impl Fn(&str) -> bool) -> bool {
    match v {
        Some(Value::Arr(a)) => {
            a.len() >= min_items && a.iter().all(|x| x.as_str().is_some_and(&item)) && (!uniq || unique(a))
        }
        _ => false,
    }
}

fn opt<T>(v: Option<&Value>, f: impl Fn(Option<&Value>) -> T) -> T
where
    T: From<bool>,
{
    if v.is_none() {
        T::from(true)
    } else {
        f(v)
    }
}

/// `action-request.schema.json`.
pub fn request_schema_valid(r: &Value) -> bool {
    const KEYS: [&str; 14] = [
        "contract_version", "request_id", "trace_id", "tenant_id", "principal", "project_id", "session_id",
        "agent_id", "parent_session_id", "idempotency_key", "expected_state_revision", "policy_bundle_sha",
        "deadline_at", "action",
    ];
    const REQUIRED: [&str; 9] = [
        "contract_version", "request_id", "trace_id", "tenant_id", "principal", "project_id", "session_id",
        "idempotency_key", "action",
    ];
    if r.as_obj().is_none() || !only_keys(r, &KEYS) || !has_keys(r, &REQUIRED) {
        return false;
    }
    if r.str_at("contract_version") != Some("2.0") {
        return false;
    }
    if !(str_len(r.get("request_id"), 1, 128) && str_len(r.get("trace_id"), 1, 128) && str_len(r.get("tenant_id"), 1, 128)) {
        return false;
    }
    let p = r.get("principal").unwrap();
    if p.as_obj().is_none() || !only_keys(p, &["id", "verified_by", "authn_method", "roles"]) || !has_keys(p, &["id", "verified_by", "authn_method"]) {
        return false;
    }
    if !(str_len(p.get("id"), 1, usize::MAX) && str_len(p.get("verified_by"), 1, usize::MAX) && str_len(p.get("authn_method"), 1, usize::MAX)) {
        return false;
    }
    if !opt(p.get("roles"), |v| str_array(v, 0, true, |_| true)) {
        return false;
    }
    if !(str_len(r.get("project_id"), 1, usize::MAX) && str_len(r.get("session_id"), 1, usize::MAX)) {
        return false;
    }
    if !(opt_str_or_null(r.get("agent_id")) && opt_str_or_null(r.get("parent_session_id")) && opt_str_or_null(r.get("policy_bundle_sha"))) {
        return false;
    }
    if !str_len(r.get("idempotency_key"), 1, 256) {
        return false;
    }
    match r.get("expected_state_revision") {
        None | Some(Value::Null) => {}
        Some(Value::Int(n)) if (0..=json::MAX_SAFE).contains(n) => {}
        _ => return false,
    }
    match r.get("deadline_at") {
        None | Some(Value::Null) => {}
        Some(Value::Str(t)) if time::parse_rfc3339(t).is_some() => {}
        _ => return false,
    }
    let a = r.get("action").unwrap();
    if a.as_obj().is_none()
        || !only_keys(a, &["kind", "resource", "action_digest", "attributes", "requested_capabilities"])
        || !has_keys(a, &["kind", "resource", "action_digest"])
    {
        return false;
    }
    if !a.str_at("kind").is_some_and(is_kind) {
        return false;
    }
    let res = a.get("resource").unwrap();
    if res.as_obj().is_none() || !only_keys(res, &["uri", "canonical_resource_id"]) || !str_len(res.get("uri"), 1, usize::MAX) {
        return false;
    }
    if !opt_str_or_null(res.get("canonical_resource_id")) {
        return false;
    }
    if !str_len(a.get("action_digest"), 16, usize::MAX) {
        return false;
    }
    if !matches!(a.get("attributes"), None | Some(Value::Obj(_))) {
        return false;
    }
    opt(a.get("requested_capabilities"), |v| str_array(v, 0, true, |_| true))
}

fn statement_schema_valid(st: &Value) -> bool {
    const KEYS: [&str; 9] = [
        "statement_id", "effect", "tenants", "principal_roles", "action_kinds", "resource_prefixes", "reason_code",
        "from_states", "to_states",
    ];
    if st.as_obj().is_none() || !only_keys(st, &KEYS) || !has_keys(st, &["statement_id", "effect", "action_kinds", "reason_code"]) {
        return false;
    }
    str_len(st.get("statement_id"), 1, usize::MAX)
        && matches!(st.str_at("effect"), Some("forbid" | "require_approval" | "permit"))
        && opt(st.get("tenants"), |v| str_array(v, 0, true, |x| !x.is_empty()))
        && opt(st.get("principal_roles"), |v| str_array(v, 0, true, |x| !x.is_empty()))
        && str_array(st.get("action_kinds"), 1, true, |x| !x.is_empty())
        && opt(st.get("resource_prefixes"), |v| str_array(v, 0, true, |x| !x.is_empty()))
        && st.str_at("reason_code").is_some_and(is_reason_code)
        && opt(st.get("from_states"), |v| str_array(v, 1, true, is_name))
        && opt(st.get("to_states"), |v| str_array(v, 1, true, is_name))
}

fn protection_schema_valid(p: &Value) -> bool {
    if p.as_obj().is_none()
        || !only_keys(p, &["acceptance_policy_bundle_sha", "protected_to_states", "protected_transitions"])
        || !has_keys(p, &["acceptance_policy_bundle_sha", "protected_to_states", "protected_transitions"])
    {
        return false;
    }
    p.str_at("acceptance_policy_bundle_sha").is_some_and(is_digest)
        && str_array(p.get("protected_to_states"), 0, false, is_name)
        && match p.get("protected_transitions") {
            Some(Value::Arr(edges)) => edges.iter().all(|e| {
                e.as_obj().is_some()
                    && only_keys(e, &["from_state", "to_state"])
                    && e.str_at("from_state").is_some_and(is_name)
                    && e.str_at("to_state").is_some_and(is_name)
            }),
            _ => false,
        }
}

/// `policy-bundle.schema.json`.
fn bundle_schema_valid(b: &Value) -> bool {
    const KEYS: [&str; 10] = [
        "contract_version", "bundle_id", "version", "publisher", "description", "known_capabilities",
        "trusted_principal_issuers", "statements", "publisher_signature", "workflow_protection",
    ];
    const REQUIRED: [&str; 7] = [
        "contract_version", "bundle_id", "version", "publisher", "known_capabilities", "trusted_principal_issuers", "statements",
    ];
    if b.as_obj().is_none() || !only_keys(b, &KEYS) || !has_keys(b, &REQUIRED) {
        return false;
    }
    b.str_at("contract_version") == Some("2.0")
        && str_len(b.get("bundle_id"), 1, usize::MAX)
        && str_len(b.get("version"), 1, usize::MAX)
        && str_len(b.get("publisher"), 1, usize::MAX)
        && opt_str_or_null(b.get("description"))
        && str_array(b.get("known_capabilities"), 1, true, is_kind)
        && str_array(b.get("trusted_principal_issuers"), 1, true, |x| !x.is_empty())
        && matches!(b.get("statements"), Some(Value::Arr(a)) if a.iter().all(statement_schema_valid))
        && opt_str_or_null(b.get("publisher_signature"))
        && opt(b.get("workflow_protection"), |v| v.is_some_and(protection_schema_valid))
}

// ---------------------------------------------------------------------------
// Bundle loading (fail closed).

#[derive(Clone, Debug)]
pub struct Statement {
    pub id: String,
    pub effect: String,
    pub tenants: Option<Vec<String>>,
    pub roles: Option<Vec<String>>,
    pub kinds: Vec<String>,
    pub prefixes: Option<Vec<String>>,
    pub reason: String,
    pub from_states: Option<Vec<String>>,
    pub to_states: Option<Vec<String>>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Protection {
    pub to_states: Vec<String>,
    pub edges: Vec<(String, String)>,
    pub acceptance_policy_bundle_sha: String,
}

impl Protection {
    /// Union rule (D-019 N-4): destination protection from any source, plus
    /// source-specific edges.
    pub fn is_protected(&self, from: &str, to: &str) -> bool {
        self.to_states.iter().any(|x| x == to) || self.edges.iter().any(|(f, t)| f == from && t == to)
    }
}

#[derive(Clone, Debug)]
pub struct Bundle {
    pub sha: String,
    pub known: Vec<String>,
    pub issuers: Vec<String>,
    pub statements: Vec<Statement>,
    pub protection: Option<Protection>,
}

fn strings(v: Option<&Value>) -> Option<Vec<String>> {
    v.and_then(Value::as_arr).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
}

/// Digest of the bundle content; the publisher signature is not part of it.
pub fn bundle_sha(doc: &Value) -> Option<String> {
    json::digest(&doc.without("publisher_signature")).ok()
}

pub fn load_bundle(doc: &Value) -> Result<Bundle, ()> {
    if !bundle_schema_valid(doc) {
        return Err(());
    }
    let sha = bundle_sha(doc).ok_or(())?;
    let known = strings(doc.get("known_capabilities")).unwrap();
    let statements: Vec<Statement> = doc
        .get("statements")
        .and_then(Value::as_arr)
        .unwrap()
        .iter()
        .map(|st| Statement {
            id: st.str_at("statement_id").unwrap().to_string(),
            effect: st.str_at("effect").unwrap().to_string(),
            tenants: strings(st.get("tenants")),
            roles: strings(st.get("principal_roles")),
            kinds: strings(st.get("action_kinds")).unwrap(),
            prefixes: strings(st.get("resource_prefixes")),
            reason: st.str_at("reason_code").unwrap().to_string(),
            from_states: strings(st.get("from_states")),
            to_states: strings(st.get("to_states")),
        })
        .collect();
    let mut ids = HashSet::new();
    if !statements.iter().all(|st| ids.insert(st.id.clone())) {
        return Err(());
    }
    if statements.iter().any(|st| st.kinds.iter().any(|k| !known.contains(k))) {
        return Err(());
    }
    let covers_transition = |st: &Statement| st.kinds.iter().any(|k| k == TRANSITION);
    // Permit or approval for transitions must name the target states.
    if statements.iter().any(|st| st.effect != "forbid" && covers_transition(st) && st.to_states.is_none()) {
        return Err(());
    }
    let protection = match doc.get("workflow_protection") {
        None => None,
        Some(p) => {
            let to_states = strings(p.get("protected_to_states")).unwrap();
            let edges: Vec<(String, String)> = p
                .get("protected_transitions")
                .and_then(Value::as_arr)
                .unwrap()
                .iter()
                .map(|e| (e.str_at("from_state").unwrap().to_string(), e.str_at("to_state").unwrap().to_string()))
                .collect();
            let mut seen_states = HashSet::new();
            if !to_states.iter().all(|x| seen_states.insert(x.clone())) {
                return Err(());
            }
            let mut seen_edges = HashSet::new();
            if !edges.iter().all(|e| e.0 != e.1 && seen_edges.insert(e.clone())) {
                return Err(());
            }
            Some(Protection { to_states, edges, acceptance_policy_bundle_sha: p.str_at("acceptance_policy_bundle_sha").unwrap().to_string() })
        }
    };
    if statements.iter().any(covers_transition) {
        match &protection {
            None => return Err(()),
            Some(p) => {
                if edge_bypassable(&statements, p) {
                    return Err(());
                }
            }
        }
    }
    let issuers = strings(doc.get("trusted_principal_issuers")).unwrap();
    Ok(Bundle { sha, known, issuers, statements, protection })
}

/// D-019 N-4: a protected edge A->B (B not a protected state) must not be
/// reachable from A through transitions permitted or approvable by statements
/// that use no protected edge and enter no protected state. A statement
/// without `from_states` permits any source; states are all names in the
/// bundle. Tenant, role and resource conditions are ignored (conservative).
fn edge_bypassable(statements: &[Statement], p: &Protection) -> bool {
    let mut states: BTreeSet<String> = BTreeSet::new();
    for st in statements {
        for list in [&st.from_states, &st.to_states].into_iter().flatten() {
            states.extend(list.iter().cloned());
        }
    }
    states.extend(p.to_states.iter().cloned());
    for (f, t) in &p.edges {
        states.insert(f.clone());
        states.insert(t.clone());
    }
    let mut graph: BTreeSet<(String, String)> = BTreeSet::new();
    for st in statements.iter().filter(|st| st.effect != "forbid" && st.kinds.iter().any(|k| k == TRANSITION)) {
        let froms: Vec<String> = st.from_states.clone().unwrap_or_else(|| states.iter().cloned().collect());
        for f in &froms {
            for t in st.to_states.iter().flatten() {
                let edge = (f.clone(), t.clone());
                if f != t && !p.edges.contains(&edge) && !p.to_states.contains(t) {
                    graph.insert(edge);
                }
            }
        }
    }
    p.edges.iter().filter(|(_, b)| !p.to_states.contains(b)).any(|(a, b)| {
        let mut seen: HashSet<&str> = HashSet::from([a.as_str()]);
        let mut queue: VecDeque<&str> = VecDeque::from([a.as_str()]);
        while let Some(x) = queue.pop_front() {
            for (f, t) in graph.iter() {
                if f == x && seen.insert(t.as_str()) {
                    if t == b {
                        return true;
                    }
                    queue.push_back(t.as_str());
                }
            }
        }
        false
    })
}

// ---------------------------------------------------------------------------
// Decision.

pub enum Policy<'a> {
    Absent,
    Rejected,
    Loaded(&'a Bundle),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    pub status: &'static str,
    pub reason: String,
    pub matched: Vec<String>,
    pub approval_request_id: Option<String>,
    pub policy_sha: String,
    pub request_id: String,
    pub state_revision: i64,
    pub decided_at: Instant,
    pub expires_at: Option<Instant>,
}

impl Decision {
    pub fn to_value(&self) -> Value {
        let decision_id = json::digest(&obj(vec![
            ("request", s(&self.request_id)),
            ("policy", s(&self.policy_sha)),
            ("revision", Value::Int(self.state_revision)),
            ("status", s(self.status)),
            ("reason", s(&self.reason)),
            ("at", s(&time::format(self.decided_at))),
        ]))
        .map(|d| format!("dec:{}", &d[7..39]))
        .unwrap_or_else(|_| "dec:unknown".into());
        obj(vec![
            ("contract_version", s("2.0")),
            ("decision_id", s(&decision_id)),
            ("request_id", s(&self.request_id)),
            ("status", s(self.status)),
            ("policy_bundle_sha", s(&self.policy_sha)),
            ("reason_code", s(&self.reason)),
            ("safe_message", Value::Null),
            ("matched_rule_ids", Value::Arr(self.matched.iter().map(|x| s(x)).collect())),
            ("state_revision", Value::Int(self.state_revision)),
            ("decided_at", s(&time::format(self.decided_at))),
            ("permit_reference", Value::Null),
            ("approval_request_reference", self.approval_request_id.as_deref().map(s).unwrap_or(Value::Null)),
            ("expires_at", self.expires_at.map(|t| s(&time::format(t))).unwrap_or(Value::Null)),
        ])
    }
}

fn is_canonical_id(rid: &str) -> bool {
    let Some((scheme, rest)) = rid.split_once(':') else { return false };
    if scheme.is_empty() || rest.is_empty() || rest.contains('\\') {
        return false;
    }
    let segs: Vec<&str> = rest.split('/').collect();
    segs.len() >= 3 && segs.iter().all(|x| !matches!(*x, "" | "." | ".."))
}

fn in_scope(rid: &str, tenant: &str, project: &str) -> bool {
    if tenant.contains('/') || project.contains('/') {
        return false;
    }
    rid.split_once(':').is_some_and(|(_, rest)| rest.starts_with(&format!("{tenant}/{project}/")))
}

/// Sorted, unique requested capability set (SR-02).
pub fn capability_set(req: &Value) -> Vec<String> {
    let set: BTreeSet<String> = strings(req.get("action").and_then(|a| a.get("requested_capabilities"))).unwrap_or_default().into_iter().collect();
    set.into_iter().collect()
}

/// Deterministic approval request id: binds the exact operation and policy
/// version; for transitions also `from_state` and `to_state`.
pub fn approval_request_id(req: &Value, bundle_sha: &str) -> String {
    let action = req.get("action").cloned().unwrap_or(Value::Null);
    let mut binding = vec![
        ("tenant_id", req.get("tenant_id").cloned().unwrap_or(Value::Null)),
        ("principal_id", req.get("principal").and_then(|p| p.get("id")).cloned().unwrap_or(Value::Null)),
        ("action_kind", action.get("kind").cloned().unwrap_or(Value::Null)),
        ("canonical_resource_id", action.get("resource").and_then(|r| r.get("canonical_resource_id")).cloned().unwrap_or(Value::Null)),
        ("action_digest", action.get("action_digest").cloned().unwrap_or(Value::Null)),
        ("requested_capabilities", Value::Arr(capability_set(req).iter().map(|x| s(x)).collect())),
        ("policy_bundle_sha", s(bundle_sha)),
    ];
    if action.str_at("kind") == Some(TRANSITION) {
        let attrs = action.get("attributes");
        binding.push(("from_state", attrs.and_then(|a| a.get("from_state")).cloned().unwrap_or(Value::Null)));
        binding.push(("to_state", attrs.and_then(|a| a.get("to_state")).cloned().unwrap_or(Value::Null)));
    }
    let d = json::digest(&obj(binding)).unwrap_or_else(|_| "sha256:".to_string() + &"0".repeat(64));
    format!("apr:{}", &d[7..39])
}

fn statement_applies(st: &Statement, req: &Value, kinds: &BTreeSet<String>, transition: Option<(&str, &str)>) -> bool {
    if let Some(ts) = &st.tenants {
        if !req.str_at("tenant_id").is_some_and(|t| ts.iter().any(|x| x == t)) {
            return false;
        }
    }
    if let Some(rs) = &st.roles {
        let held = strings(req.get("principal").and_then(|p| p.get("roles"))).unwrap_or_default();
        if !rs.iter().any(|r| held.contains(r)) {
            return false;
        }
    }
    if !st.kinds.iter().any(|k| kinds.contains(k)) {
        return false;
    }
    if let Some(ps) = &st.prefixes {
        let rid = req.get("action").and_then(|a| a.get("resource")).and_then(|r| r.str_at("canonical_resource_id")).unwrap_or("");
        if !ps.iter().any(|p| rid.starts_with(p.as_str())) {
            return false;
        }
    }
    if st.from_states.is_some() || st.to_states.is_some() {
        let Some((from, to)) = transition else { return false };
        if st.from_states.as_ref().is_some_and(|l| !l.iter().any(|x| x == from)) {
            return false;
        }
        if st.to_states.as_ref().is_some_and(|l| !l.iter().any(|x| x == to)) {
            return false;
        }
    }
    true
}

/// Validated transition attributes.
pub struct TransitionAttrs<'a> {
    pub from: &'a str,
    pub to: &'a str,
    pub bound: bool,
}

pub fn transition_attrs(action: &Value) -> Option<TransitionAttrs<'_>> {
    let attrs = action.get("attributes")?;
    let m = attrs.as_obj()?;
    let allowed = ["from_state", "to_state", ACCEPTANCE_ATTRS[0], ACCEPTANCE_ATTRS[1], ACCEPTANCE_ATTRS[2]];
    if !m.iter().all(|(k, _)| allowed.contains(&k.as_str())) {
        return None;
    }
    let from = attrs.str_at("from_state").filter(|x| is_name(x))?;
    let to = attrs.str_at("to_state").filter(|x| is_name(x))?;
    if from == to {
        return None;
    }
    let present: Vec<bool> = ACCEPTANCE_ATTRS.iter().map(|k| attrs.get(k).is_some()).collect();
    let bound = present.iter().all(|x| *x);
    if !bound && present.iter().any(|x| *x) {
        return None;
    }
    if bound && !ACCEPTANCE_ATTRS.iter().all(|k| attrs.str_at(k).is_some()) {
        return None;
    }
    Some(TransitionAttrs { from, to, bound })
}

/// `workflow:<tenant>/<project>/<workflow_id>` exactly.
pub fn workflow_resource_valid(rid: &str, tenant: &str, project: &str) -> bool {
    let Some(rest) = rid.strip_prefix("workflow:") else { return false };
    let segs: Vec<&str> = rest.split('/').collect();
    segs.len() == 3 && segs[0] == tenant && segs[1] == project && is_name(segs[2])
}

/// The pure decision function.
pub fn decide(req: &Value, policy: Policy<'_>, state_revision: i64, now: Instant) -> Decision {
    let request_id = req.str_at("request_id").filter(|x| !x.is_empty()).unwrap_or("unknown").to_string();
    let policy_sha = match &policy {
        Policy::Loaded(b) => b.sha.clone(),
        _ => "none".to_string(),
    };
    let mut deadline: Option<Instant> = None;
    let mk = |status: &'static str, reason: &str, matched: Vec<String>, apr: Option<String>, deadline: Option<Instant>| {
        let mut matched = matched;
        matched.sort();
        let expires_at = if matches!(status, "allow" | "require_approval") {
            let ttl = time::add_seconds(now, DECISION_TTL_SECONDS);
            Some(deadline.map_or(ttl, |d| d.min(ttl)))
        } else {
            None
        };
        Decision {
            status,
            reason: reason.to_string(),
            matched,
            approval_request_id: apr,
            policy_sha: policy_sha.clone(),
            request_id: request_id.clone(),
            state_revision,
            decided_at: now,
            expires_at,
        }
    };
    let none = Vec::new;

    if !request_schema_valid(req) {
        return mk("invalid", "REQUEST_SCHEMA_INVALID", none(), None, None);
    }
    let action = req.get("action").unwrap();
    let kind = action.str_at("kind").unwrap();
    let caps = strings(action.get("requested_capabilities")).unwrap_or_default();
    if caps.iter().any(|c| c == kind) {
        return mk("invalid", "REQUESTED_CAPABILITIES_INVALID", none(), None, None);
    }
    if let Some(d) = req.str_at("deadline_at") {
        let d = time::parse_rfc3339(d).unwrap();
        deadline = Some(d);
        if d <= now {
            return mk("invalid", "DEADLINE_EXCEEDED", none(), None, None);
        }
    }
    let bundle = match policy {
        Policy::Absent => return mk("unavailable", "POLICY_UNAVAILABLE", none(), None, None),
        Policy::Rejected => return mk("unavailable", "POLICY_BUNDLE_REJECTED", none(), None, None),
        Policy::Loaded(b) => b,
    };
    if let Some(pin) = req.str_at("policy_bundle_sha") {
        if pin != bundle.sha {
            return mk("unavailable", "POLICY_VERSION_MISMATCH", none(), None, None);
        }
    }
    let issuer = req.get("principal").and_then(|p| p.str_at("verified_by")).unwrap();
    if !bundle.issuers.iter().any(|x| x == issuer) {
        return mk("deny", "PRINCIPAL_UNVERIFIED", none(), None, deadline);
    }
    if let Some(expected) = req.get("expected_state_revision").and_then(Value::as_int) {
        if expected != state_revision {
            return mk("invalid", "STATE_REVISION_CONFLICT", none(), None, deadline);
        }
    }
    let mut kinds: BTreeSet<String> = BTreeSet::from([kind.to_string()]);
    kinds.extend(caps.iter().cloned());
    if !kinds.iter().all(|k| bundle.known.contains(k)) {
        return mk("deny", "UNKNOWN_CAPABILITY", none(), None, deadline);
    }
    let rid = action.get("resource").and_then(|r| r.str_at("canonical_resource_id")).unwrap_or("");
    if rid.is_empty() || !is_canonical_id(rid) {
        return mk("invalid", "RESOURCE_NOT_CANONICAL", none(), None, deadline);
    }
    let tenant = req.str_at("tenant_id").unwrap();
    let project = req.str_at("project_id").unwrap();
    if !in_scope(rid, tenant, project) {
        return mk("deny", "RESOURCE_SCOPE_MISMATCH", none(), None, deadline);
    }

    let mut transition: Option<(&str, &str)> = None;
    if kind == TRANSITION {
        if !workflow_resource_valid(rid, tenant, project) {
            return mk("invalid", "WORKFLOW_RESOURCE_INVALID", none(), None, deadline);
        }
        let Some(t) = transition_attrs(action) else {
            return mk("invalid", "TRANSITION_MALFORMED", none(), None, deadline);
        };
        if !caps.is_empty() {
            return mk("invalid", "COMPOSITE_NOT_SUPPORTED", none(), None, deadline);
        }
        // Missing classification fails closed (D-019 N-1). Unreachable for a
        // bundle with transition statements, which is rejected at load.
        let Some(p) = &bundle.protection else {
            return mk("unavailable", "PROTECTION_UNAVAILABLE", none(), None, deadline);
        };
        let protected = p.is_protected(t.from, t.to);
        if protected && !t.bound {
            return mk("invalid", "ACCEPTANCE_BINDING_REQUIRED", none(), None, deadline);
        }
        if !protected && t.bound {
            return mk("invalid", "ACCEPTANCE_BINDING_UNEXPECTED", none(), None, deadline);
        }
        transition = Some((t.from, t.to));
    }

    let mut matching: Vec<&Statement> = bundle.statements.iter().filter(|st| statement_applies(st, req, &kinds, transition)).collect();
    matching.sort_by(|a, b| a.id.as_bytes().cmp(b.id.as_bytes()));
    let ids = |v: &[&Statement]| v.iter().map(|st| st.id.clone()).collect::<Vec<_>>();
    let forbids: Vec<&Statement> = matching.iter().copied().filter(|st| st.effect == "forbid").collect();
    if let Some(first) = forbids.first() {
        return mk("deny", &first.reason, ids(&forbids), None, deadline);
    }
    let approvals: Vec<&Statement> = matching.iter().copied().filter(|st| st.effect == "require_approval").collect();
    let permits: Vec<&Statement> = matching.iter().copied().filter(|st| st.effect == "permit").collect();
    if approvals.is_empty() && permits.is_empty() {
        return mk("deny", "DEFAULT_DENY", none(), None, deadline);
    }
    let both: Vec<&Statement> = approvals.iter().chain(permits.iter()).copied().collect();
    let covered: BTreeSet<&String> = both.iter().flat_map(|st| st.kinds.iter()).collect();
    if !kinds.iter().all(|k| covered.contains(k)) {
        return mk("deny", "CAPABILITY_NOT_PERMITTED", ids(&both), None, deadline);
    }
    if let Some(first) = approvals.first() {
        let apr = approval_request_id(req, &bundle.sha);
        return mk("require_approval", &first.reason, ids(&approvals), Some(apr), deadline);
    }
    mk("allow", &permits[0].reason, ids(&permits), None, deadline)
}

/// `decide` command input: `{"bundle", "state_revision", "now", "request"}`.
pub fn decide_command(input: &Value) -> Value {
    let invalid = |reason: &str| {
        obj(vec![("status", s("invalid")), ("reason_code", s(reason)), ("matched_rule_ids", Value::Arr(vec![]))])
    };
    let (Some(now), Some(rev), Some(req)) = (
        input.str_at("now").and_then(time::parse_rfc3339),
        input.get("state_revision").and_then(Value::as_int),
        input.get("request"),
    ) else {
        return invalid("REQUEST_SCHEMA_INVALID");
    };
    let loaded;
    let policy = match input.get("bundle") {
        None | Some(Value::Null) => Policy::Absent,
        Some(doc) => match load_bundle(doc) {
            Ok(b) => {
                loaded = b;
                Policy::Loaded(&loaded)
            }
            Err(()) => Policy::Rejected,
        },
    };
    decide(req, policy, rev, now).to_value()
}
