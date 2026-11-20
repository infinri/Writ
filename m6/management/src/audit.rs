//! Bounded read-only projection of owner-recorded events. The embedding service
//! configures the replica. Request fields cannot choose the database or scope.
use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use rusqlite::{limits::Limit, params_from_iter, types::Value as SqlValue, Connection, OpenFlags};
use writ_kernel::{
    ids::{is_digest, is_reason_code},
    json::{self, Map, Value},
    time::Timestamp,
};

use crate::{Error, Result, Scope, Store};

pub const REPLICA_SCHEMA: &str = include_str!("audit_replica.sql");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AuditOperation {
    Search,
    Timeline,
    Explain,
}

pub struct AuditReader {
    source: AuditSource,
}

enum AuditSource {
    Replica(PathBuf),
    DraftOwner(Store),
}

impl AuditReader {
    pub fn new(path: &Path) -> Self {
        Self {
            source: AuditSource::Replica(path.into()),
        }
    }

    pub fn draft_owner(store: &Store) -> Self {
        Self {
            source: AuditSource::DraftOwner(store.clone()),
        }
    }

    pub fn query(
        &self,
        scope: &Scope,
        operation: AuditOperation,
        mut fields: Map,
    ) -> Result<Value> {
        let explain = operation == AuditOperation::Explain;
        let relation = match self.source {
            AuditSource::Replica(_) => "audit_events",
            AuditSource::DraftOwner(_) => include_str!("audit_draft_owner.sql"),
        };
        let mut sql = format!("SELECT sequence, event_id, event_type, occurred_at, trace_id, request_id,
            principal_id, action_kind, status, policy_version, payload FROM {relation} WHERE tenant=? AND project=?");
        let mut args: Vec<SqlValue> =
            vec![scope.tenant.clone().into(), scope.project.clone().into()];
        let limit = if explain {
            if !fields.contains_key("event_id") {
                return Err(Error::InvalidRequest);
            }
            1
        } else {
            let after = integer(&mut fields, "after", 0, 0, json::MAX_SAFE_INTEGER)?;
            sql.push_str(" AND sequence > ?");
            args.push(after.into());
            integer(&mut fields, "limit", 50, 1, 500)?
        };
        let filters: &[(&str, &str)] = match operation {
            AuditOperation::Explain => &[("event_id", "event_id=?")],
            AuditOperation::Timeline => {
                if fields.contains_key("trace_id") == fields.contains_key("request_id") {
                    return Err(Error::InvalidRequest);
                }
                &[("trace_id", "trace_id=?"), ("request_id", "request_id=?")]
            }
            AuditOperation::Search => &[
                ("event_type", "event_type=?"),
                ("trace_id", "trace_id=?"),
                ("request_id", "request_id=?"),
                ("principal_id", "principal_id=?"),
                ("action_kind", "action_kind=?"),
                ("status", "status=?"),
                ("policy_version", "policy_version=?"),
                ("since", "occurred_at>=?"),
                ("until", "occurred_at<=?"),
            ],
        };
        for &(field, predicate) in filters {
            if let Some(value) = fields.remove(field) {
                let value = value
                    .as_str()
                    .filter(|s| token(s))
                    .ok_or(Error::InvalidRequest)?;
                if matches!(field, "since" | "until") && Timestamp::parse_strict(value).is_none() {
                    return Err(Error::InvalidRequest);
                }
                sql.push_str(" AND ");
                sql.push_str(predicate);
                args.push(value.to_string().into());
            }
        }
        if !fields.is_empty() {
            return Err(Error::InvalidRequest);
        }
        sql.push_str(" ORDER BY sequence LIMIT ?");
        args.push((limit + 1).into());

        let db = match &self.source {
            AuditSource::DraftOwner(store) => store.connection(false)?,
            AuditSource::Replica(path) => open_replica(path)?,
        };
        let mut query = db.prepare(&sql).map_err(unavailable)?;
        let mut rows = query.query(params_from_iter(args)).map_err(unavailable)?;
        let mut events = Vec::new();
        let mut cursor = Value::Null;
        let mut last = 0;
        while let Some(row) = rows.next().map_err(unavailable)? {
            if events.len() == limit as usize {
                cursor = Value::Int(last);
                break;
            }
            last = row.get(0).map_err(unavailable)?;
            let result = project(row, last)?;
            if explain {
                return Ok(result);
            }
            events.push(result);
        }
        if explain {
            return Err(Error::NotFound);
        }
        Ok(json::obj([
            ("events", Value::Arr(events)),
            ("next", cursor),
        ]))
    }
}

fn open_replica(path: &Path) -> Result<Connection> {
    if !std::fs::symlink_metadata(path).is_ok_and(|m| m.is_file()) {
        return Err(Error::Unavailable);
    }
    let mut db = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )
    .map_err(unavailable)?;
    db.busy_timeout(Duration::from_millis(100))
        .map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_LENGTH, 32 * 1024)
        .map_err(unavailable)?;
    db.set_limit(Limit::SQLITE_LIMIT_ATTACHED, 0)
        .map_err(unavailable)?;
    let start = Instant::now();
    db.progress_handler(500, Some(move || start.elapsed() >= Duration::from_secs(5)))
        .map_err(unavailable)?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA trusted_schema=OFF; PRAGMA mmap_size=0")
        .map_err(unavailable)?;
    Ok(db)
}

fn unavailable(_: rusqlite::Error) -> Error {
    Error::Unavailable
}

fn integer(fields: &mut Map, key: &str, default: i64, min: i64, max: i64) -> Result<i64> {
    match fields.remove(key) {
        None => Ok(default),
        Some(Value::Int(x)) if (min..=max).contains(&x) => Ok(x),
        _ => Err(Error::InvalidRequest),
    }
}

fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

fn project(row: &rusqlite::Row<'_>, sequence: i64) -> Result<Value> {
    let mut out = Map::new();
    out.insert("sequence".into(), Value::Int(sequence));
    for (i, name) in [
        "event_id",
        "event_type",
        "occurred_at",
        "trace_id",
        "request_id",
        "principal_id",
        "action_kind",
        "status",
        "policy_version",
    ]
    .into_iter()
    .enumerate()
    {
        if let Some(value) = row.get::<_, Option<String>>(i + 1).map_err(unavailable)? {
            if !token(&value)
                || (name == "occurred_at" && Timestamp::parse_strict(&value).is_none())
            {
                return Err(Error::Unavailable);
            }
            out.insert(name.into(), Value::Str(value));
        }
    }
    let payload: String = row.get(10).map_err(unavailable)?;
    let Value::Obj(record) = json::parse(payload.as_bytes()).map_err(|_| Error::Unavailable)?
    else {
        return Err(Error::Unavailable);
    };
    let kind = out
        .get("event_type")
        .and_then(Value::as_str)
        .ok_or(Error::Unavailable)?;
    let status = out.get("status").and_then(Value::as_str).unwrap_or("");
    let explanation = explanation(kind, status, &record)?;
    out.insert("explanation".into(), explanation);
    Ok(Value::Obj(out))
}

fn explanation(kind: &str, status: &str, record: &Map) -> Result<Value> {
    let mut evidence = Map::new();
    for key in [
        "reason_code",
        "policy_bundle_sha",
        "dependency",
        "component",
        "attempt_id",
        "permit_id",
        "grant_id",
        "decision_id",
        "receipt_id",
        "source_audit_id",
        "draft_id",
        "previous_revision",
        "request_digest",
        "content_digest",
        "causation_id",
        "state_revision",
        "deadline_at",
        "approval_role",
        "action_digest",
        "result_digest",
        "recovery_code",
        "matched_rule_ids",
        "deciding_rule_id",
        "rule_precedence",
        "policy_version",
        "approval_state",
        "transaction_name",
        "failure_stage",
        "progress",
        "possible_progress",
        "outcome_unknown",
        "request_fingerprint",
        "span_sampled",
    ] {
        let Some(value) = record.get(key) else {
            continue;
        };
        if value == &Value::Null {
            continue;
        }
        let valid = match key {
            "outcome_unknown" | "span_sampled" => matches!(value, Value::Bool(_)),
            "source_audit_id" | "previous_revision" | "state_revision" => {
                matches!(value, Value::Int(x) if *x >= 0)
            }
            "matched_rule_ids" => matches!(value, Value::Arr(ids) if ids.len() <= 64 &&
                ids.iter().all(|id| id.as_str().is_some_and(token))),
            "policy_bundle_sha"
            | "action_digest"
            | "result_digest"
            | "request_fingerprint"
            | "request_digest"
            | "content_digest" => value.as_str().is_some_and(is_digest),
            "reason_code" | "recovery_code" => value
                .as_str()
                .is_some_and(|v| token(v) && is_reason_code(v)),
            "deadline_at" => value
                .as_str()
                .is_some_and(|v| Timestamp::parse_rfc3339(v).is_some()),
            _ => value.as_str().is_some_and(token),
        };
        if !valid {
            return Err(Error::Unavailable);
        }
        evidence.insert(key.into(), value.clone());
    }
    let summary = match (kind, status) {
        ("decision", "allow") => "Policy permitted the request.",
        ("decision", "deny") => "Policy denied the request.",
        ("decision", "require_approval") => "The decision requires approval.",
        ("decision", "unavailable") => {
            "A decision dependency was unavailable. Execution was refused."
        }
        ("decision", "invalid") => "Decision input failed validation.",
        ("effect.intent", "reserved") => "INTENT",
        ("effect.dispatch", "dispatched") => "DISPATCHED",
        ("effect.receipt" | "effect.reconcile", "observed_success") => "COMPLETED",
        ("effect.receipt" | "effect.reconcile", "observed_failure") => "FAILED",
        ("effect.reconcile", "unknown_reconcile") | ("effect.interrupted", "dispatched") => {
            "UNKNOWN_RECONCILE"
        }
        ("effect.refusal", "refused") => "REFUSED",
        ("management.mutation", "committed") => {
            "The draft owner committed the version and its audit receipt together."
        }
        ("failure", _) => "The component recorded a failure.",
        (
            "approval" | "adapter" | "trace" | "log" | "policy.evaluation" | "management.mutation",
            _,
        ) => "Recorded event.",
        _ => return Err(Error::Unavailable),
    };
    if kind == "decision" {
        let reason = evidence
            .get("reason_code")
            .and_then(Value::as_str)
            .ok_or(Error::Unavailable)?;
        let condition = match reason {
            "DEFAULT_DENY" => Some("No policy rule permitted this action."),
            "UNKNOWN_CAPABILITY" => Some("The request named an unknown capability."),
            "PRINCIPAL_UNVERIFIED" => Some("The principal proof was missing or unverified."),
            "STATE_REVISION_CONFLICT" => Some("The expected and recorded state revisions differ."),
            "POLICY_VERSION_MISMATCH" => {
                Some("The requested policy digest differs from the loaded policy.")
            }
            "POLICY_UNAVAILABLE" => Some("The policy bundle was unavailable."),
            "REQUEST_SCHEMA_INVALID" => Some("The request failed schema validation."),
            "RESOURCE_NOT_CANONICAL" => {
                Some("The request lacked a valid canonical resource identifier.")
            }
            "RESOURCE_SCOPE_MISMATCH" => Some("The resource was outside the request scope."),
            "CAPABILITY_NOT_PERMITTED" => {
                Some("The requested capability lacked a permitting rule.")
            }
            "DEADLINE_EXCEEDED" => Some("The decision deadline had passed."),
            _ => None,
        };
        if let Some(condition) = condition {
            evidence.insert("condition".into(), json::s(condition));
        }
    }
    if kind.starts_with("effect.") {
        evidence.insert("effect_state".into(), json::s(summary));
        evidence.insert(
            "executed".into(),
            match summary {
                "COMPLETED" => Value::Bool(true),
                "UNKNOWN_RECONCILE" | "DISPATCHED" | "FAILED" => Value::Null,
                _ => Value::Bool(false),
            },
        );
    } else {
        evidence.insert("summary".into(), json::s(summary));
    }
    Ok(Value::Obj(evidence))
}
