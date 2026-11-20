//! Read-only policy comparisons at a verified selection and a recorded evaluation context.
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::Read,
    path::Path,
    time::{Duration, Instant},
};

use crate::{evaluation, Error, PublicationBoundary, Result, Scope};
use writ_kernel::{
    acceptance,
    contracts::{AcceptanceRecord, ActionRequest, CurrentState, StateRevision},
    decision::in_scope,
    ids,
    json::{self, Value},
    time::Timestamp,
    transition,
};
use writ_policy_publication_lab::{simulation_requirements, Publication};

const MAX_BYTES: usize = 512 * 1024;
const MAX_RECORDS: usize = 128;

struct Capture {
    request: Value,
    revision: StateRevision,
    at: Timestamp,
    record: AcceptanceRecord,
    current: CurrentState,
}

/// Host-selected bounded capture. The wire protocol accepts only scoped record identifiers.
pub struct RecordedTransitions(BTreeMap<(String, String, String), Capture>);

impl RecordedTransitions {
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::from(
            rustix::fs::open(
                path,
                rustix::fs::OFlags::RDONLY
                    | rustix::fs::OFlags::CLOEXEC
                    | rustix::fs::OFlags::NOFOLLOW
                    | rustix::fs::OFlags::NONBLOCK,
                rustix::fs::Mode::empty(),
            )
            .map_err(|_| Error::Unavailable)?,
        );
        if !file.metadata().is_ok_and(|metadata| metadata.is_file()) {
            return Err(Error::Unavailable);
        }
        let mut bytes = Vec::new();
        file.take(MAX_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| Error::Unavailable)?;
        Self::parse(&bytes)
    }

    pub fn parse(bytes: &[u8]) -> Result<Self> {
        if bytes.len() > MAX_BYTES {
            return Err(Error::LimitExceeded);
        }
        let document = json::parse(bytes).map_err(|_| Error::Unavailable)?;
        if document.as_obj().map(BTreeMap::len) != Some(2)
            || document.get("version").and_then(Value::as_str) != Some("writ-transition-capture/1")
        {
            return Err(Error::Unavailable);
        }
        let rows = document
            .get("records")
            .and_then(Value::as_arr)
            .ok_or(Error::Unavailable)?;
        if rows.len() > MAX_RECORDS {
            return Err(Error::LimitExceeded);
        }
        let mut records = BTreeMap::new();
        for row in rows {
            let row = row
                .as_obj()
                .filter(|fields| fields.len() == 6)
                .ok_or(Error::Unavailable)?;
            let field = |name| row.get(name).ok_or(Error::Unavailable);
            let id = field("id")?
                .as_str()
                .filter(|id| ids::is_id(id))
                .ok_or(Error::Unavailable)?;
            let raw = field("request")?;
            let request = ActionRequest::from_value(raw).map_err(|_| Error::Unavailable)?;
            let record = AcceptanceRecord::from_value(field("acceptance_record")?)
                .map_err(|_| Error::Unavailable)?;
            let current = CurrentState::from_value(field("current_state")?)
                .map_err(|_| Error::Unavailable)?;
            let revision = field("revision")?
                .as_int()
                .and_then(StateRevision::new)
                .ok_or(Error::Unavailable)?;
            let at = field("at")?
                .as_str()
                .and_then(Timestamp::parse_strict)
                .ok_or(Error::Unavailable)?;
            let resource = request
                .action
                .resource
                .canonical_resource_id
                .as_deref()
                .ok_or(Error::Unavailable)?;
            let (from, to, binding) =
                transition::transition_attributes(request.action.attributes.as_ref())
                    .ok_or(Error::Unavailable)?;
            let binding = binding.ok_or(Error::Unavailable)?;
            if request.action.kind != "workflow.transition"
                || !in_scope(resource, &current.tenant_id, &current.project_id)
                || current.tenant_id != request.tenant_id
                || current.project_id != request.project_id
                || record.tenant_id != current.tenant_id
                || record.project_id != current.project_id
                || transition::workflow_id(resource, &request.tenant_id, &request.project_id)
                    .as_deref()
                    != Some(&current.workflow_id)
                || binding.acceptance_record_id != record.acceptance_record_id
                || binding.artifact_snapshot_id != current.artifact_snapshot_id
                || binding.artifact_digest != current.artifact_digest
                || current.transition.from_state != from
                || current.transition.to_state != to
                || current.now != at
                || current.state_revision != revision
            {
                return Err(Error::Unavailable);
            }
            let key = (request.tenant_id, request.project_id, id.to_owned());
            if records
                .insert(
                    key,
                    Capture {
                        request: raw.clone(),
                        revision,
                        at,
                        record,
                        current,
                    },
                )
                .is_some()
            {
                return Err(Error::Unavailable);
            }
        }
        Ok(Self(records))
    }
}

/// A pair draft binds both payloads. Existing authorization-only drafts retain their contract.
pub(crate) fn authorization_draft(candidate: &Value) -> &Value {
    candidate.get("authorization").unwrap_or(candidate)
}

pub(crate) fn draft_matches(candidate: &Value, publication: &Publication) -> bool {
    if publication.prerequisites.get("draft") != Some(authorization_draft(candidate)) {
        return false;
    }
    if candidate.get("authorization").is_none() {
        return true;
    }
    candidate.as_obj().is_some_and(|map| map.len() == 2)
        && candidate.get("acceptance") == Some(&publication.acceptance.payload)
}

fn selected(scope: &Scope, registry: &dyn PublicationBoundary) -> Result<(i64, i64, Publication)> {
    if registry.scope().tenant() != scope.tenant || registry.scope().project() != scope.project {
        return Err(Error::NotFound);
    }
    registry
        .selected_publication()
        .map_err(|_| Error::Unavailable)
}

fn pair_draft<'a>(
    publication: &'a Publication,
    candidate: &'a Value,
) -> Result<(&'a Value, &'a Value)> {
    let active = publication
        .prerequisites
        .get("draft")
        .ok_or(Error::Unavailable)?;
    if candidate.get("authorization").is_some()
        && (candidate.as_obj().map(BTreeMap::len) != Some(2)
            || candidate.get("acceptance").is_none())
    {
        return Err(Error::InvalidPolicy);
    }
    if let Some(payload) = candidate.get("acceptance") {
        simulation_requirements(payload).map_err(|_| Error::InvalidPolicy)?;
    }
    Ok((active, authorization_draft(candidate)))
}

fn result(
    revision: i64,
    version: i64,
    publication: &Publication,
    candidate: &Value,
    comparison: Value,
) -> Result<Value> {
    let mut data = comparison.as_obj().ok_or(Error::Unavailable)?.clone();
    data.insert("selection_revision".into(), Value::Int(revision));
    data.insert("publication_version".into(), Value::Int(version));
    data.insert(
        "publication_digest".into(),
        json::s(
            publication
                .fingerprint()
                .map_err(|_| Error::Unavailable)?
                .as_str(),
        ),
    );
    data.insert(
        "acceptance_structure".into(),
        evaluation::difference(
            &publication.acceptance.payload,
            candidate
                .get("acceptance")
                .unwrap_or(&publication.acceptance.payload),
        ),
    );
    Ok(Value::Obj(data))
}

pub(crate) fn active(
    scope: &Scope,
    registry: &dyn PublicationBoundary,
    candidate: &Value,
    corpus: &Value,
) -> Result<Value> {
    let (revision, version, publication) = selected(scope, registry)?;
    let (before, after) = pair_draft(&publication, candidate)?;
    result(
        revision,
        version,
        &publication,
        candidate,
        evaluation::compare(scope, before, after, corpus)?,
    )
}

pub(crate) fn validate_record_ids(ids: &Value) -> Result<Vec<&str>> {
    let ids = ids
        .as_arr()
        .filter(|ids| !ids.is_empty())
        .ok_or(Error::InvalidRequest)?;
    if ids.len() > 16 {
        return Err(Error::LimitExceeded);
    }
    let mut seen = BTreeSet::new();
    ids.iter()
        .map(|value| {
            value
                .as_str()
                .filter(|id| ids::is_id(id) && seen.insert(*id))
                .ok_or(Error::InvalidRequest)
        })
        .collect()
}

pub(crate) fn recorded(
    scope: &Scope,
    registry: &dyn PublicationBoundary,
    records: &RecordedTransitions,
    candidate: &Value,
    record_ids: &Value,
) -> Result<Value> {
    let started = Instant::now();
    let ids = validate_record_ids(record_ids)?;
    let (revision, version, publication) = selected(scope, registry)?;
    let (before, after) = pair_draft(&publication, candidate)?;
    let structure = evaluation::difference(before, after);
    let before = evaluation::prepare(scope, before)?.map_err(|_| Error::Unavailable)?;
    let after = evaluation::prepare(scope, after)?.map_err(|_| Error::InvalidPolicy)?;
    let left =
        simulation_requirements(&publication.acceptance.payload).map_err(|_| Error::Unavailable)?;
    let right = simulation_requirements(
        candidate
            .get("acceptance")
            .unwrap_or(&publication.acceptance.payload),
    )
    .map_err(|_| Error::InvalidPolicy)?;
    let mut cases = Vec::new();
    for id in ids {
        let capture = records
            .0
            .get(&(scope.tenant.clone(), scope.project.clone(), id.into()))
            .ok_or(Error::NotFound)?;
        let left_decision = before.decide_recorded(&capture.request, capture.revision, capture.at);
        let right_decision = after.decide_recorded(&capture.request, capture.revision, capture.at);
        // Compare criterion sets on the same historical record and policy binding. No record is reissued.
        let outcome = |requirements: &writ_broker::quorum_policy::QuorumRequirements| {
            let mut requirements = requirements.ordinary_requirements()?.clone();
            requirements.policy = capture.current.policy.clone();
            let outcome = acceptance::check(&capture.record, &capture.current, &requirements);
            Some(outcome.err().map_or("SATISFIED", |refusal| refusal.code()))
        };
        let left_acceptance = outcome(&left);
        let right_acceptance = outcome(&right);
        cases.push(json::obj([
            ("record_id", json::s(id)),
            (
                "request_id",
                capture
                    .request
                    .get("request_id")
                    .cloned()
                    .ok_or(Error::Unavailable)?,
            ),
            (
                "changed",
                evaluation::difference(&left_decision, &right_decision),
            ),
            ("before", left_decision),
            ("after", right_decision),
            (
                "acceptance_changed",
                left_acceptance
                    .zip(right_acceptance)
                    .map_or(Value::Null, |(before, after)| Value::Bool(before != after)),
            ),
            (
                "criteria_before",
                json::s(left_acceptance.unwrap_or("UNVERIFIED_QUORUM")),
            ),
            (
                "criteria_after",
                json::s(right_acceptance.unwrap_or("UNVERIFIED_QUORUM")),
            ),
        ]));
        if started.elapsed() > Duration::from_secs(5) {
            return Err(Error::LimitExceeded);
        }
    }
    result(
        revision,
        version,
        &publication,
        candidate,
        json::obj([
            ("structure", structure),
            ("cases", Value::Arr(cases)),
            ("runtime_authority", Value::Bool(false)),
            (
                "acceptance_basis",
                json::s("criterion_sets_at_historical_policy_binding"),
            ),
        ]),
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn capture_refuses_duplicate_ids_and_cross_scope_evidence() {
        let source = include_bytes!("../fixtures/recorded-transitions.json");
        assert!(RecordedTransitions::parse(source).is_ok());
        let raw = json::parse(source).unwrap();
        let rows = raw.get("records").unwrap().as_arr().unwrap();
        let capture = |records| {
            json::canonical(&json::obj([
                ("version", json::s("writ-transition-capture/1")),
                ("records", Value::Arr(records)),
            ]))
            .unwrap()
        };
        assert!(matches!(
            RecordedTransitions::parse(capture(vec![rows[0].clone(), rows[0].clone()]).as_bytes()),
            Err(Error::Unavailable)
        ));
        let foreign = capture(rows.to_vec()).replacen(
            "\"project_id\":\"offline-proof\"",
            "\"project_id\":\"another\"",
            1,
        );
        assert!(matches!(
            RecordedTransitions::parse(foreign.as_bytes()),
            Err(Error::Unavailable)
        ));
        assert_eq!(
            validate_record_ids(&Value::Arr(vec![json::s("one"); 2])),
            Err(Error::InvalidRequest)
        );
    }

    #[test]
    fn capture_refuses_fifo_without_waiting_for_a_writer() {
        let path =
            std::env::temp_dir().join(format!("writ-transition-fifo-{}", std::process::id()));
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &path,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::RUSR,
            0,
        )
        .unwrap();
        let result = RecordedTransitions::open(&path);
        std::fs::remove_file(path).unwrap();
        assert!(matches!(result, Err(Error::Unavailable)));
    }
}
