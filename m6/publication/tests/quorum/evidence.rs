//! Immutable fixture attempt and separately authenticated evaluator documents.
use super::*;
use std::collections::BTreeMap;
use writ_broker::acceptance::mac_bytes;

pub(super) const RECORD_KEY: [u8; 32] = [0x51; 32];
pub(super) fn material(index: usize) -> Vec<u8> {
    vec![0x61 + index as u8; 32]
}

fn signed(doc: Value, key: &[u8]) -> Value {
    let bytes = mac_bytes(&doc).unwrap();
    let mut padded = key.to_vec();
    padded.resize(64, 0);
    let mut inner: Vec<_> = padded.iter().map(|x| x ^ 0x36).collect();
    inner.extend(bytes.bytes());
    let inner = Digest::of_bytes(&inner);
    let mut outer: Vec<_> = padded.iter().map(|x| x ^ 0x5c).collect();
    outer.extend(
        (0..64)
            .step_by(2)
            .map(|i| u8::from_str_radix(&inner.hex()[i..i + 2], 16).unwrap()),
    );
    let auth = edit(
        doc.get("auth").unwrap(),
        "mac",
        json::s(Digest::of_bytes(&outer).hex()),
    );
    edit(&doc, "auth", auth)
}

#[derive(Clone)]
pub(super) struct Attempt {
    record: Digest,
    members: BTreeMap<String, Digest>,
}

impl CurrentAttempt for Attempt {
    fn record_id(&self) -> &str {
        "future-record"
    }
    fn record_digest(&self) -> &Digest {
        &self.record
    }
    fn attempt_id(&self) -> &str {
        "attempt-1"
    }
    fn members(&self) -> &BTreeMap<String, Digest> {
        &self.members
    }
}

pub(super) struct Source {
    policy: String,
    artifact: String,
    record: Value,
    documents: BTreeMap<String, Value>,
    attempt: Attempt,
}

impl AcceptanceRecordSource for Source {
    fn record(&self, t: &str, p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        Ok(((t, p, id) == (TENANT, PROJECT, "future-record")).then(|| self.record.clone()))
    }

    fn current_attempt(
        &self,
        context: &AttemptScope<'_>,
    ) -> Result<Option<Box<dyn CurrentAttempt + '_>>, SourceUnavailable> {
        let selected = context.workflow == &key()
            && context.revision == 0
            && context.task_id == "task"
            && context.artifact_snapshot_id == "snapshot"
            && context.artifact_digest == self.artifact
            && context.policy_sha == self.policy
            && context.from_state == "verifying"
            && context.to_state == "accepted";
        Ok(selected.then(|| Box::new(self.attempt.clone()) as Box<dyn CurrentAttempt>))
    }
}

impl AttestationSource for Source {
    fn attestation(&self, t: &str, p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        Ok(if (t, p) == (TENANT, PROJECT) {
            self.documents.get(id).cloned()
        } else {
            None
        })
    }
}

fn assign(doc: &mut Value, key: &str, value: Value) {
    *doc = edit(doc, key, value);
}

impl Source {
    pub(super) fn new(policy: &str, artifact: &str, case: Case) -> Self {
        let mut envelope = json::parse(include_bytes!("../../../../docs/contracts/proposed/examples/authenticated-acceptance-record-2.valid.json")).unwrap();
        let mut record = envelope.get("record").unwrap().clone();
        let bound = [
            ("tenant_id", TENANT),
            ("project_id", PROJECT),
            ("workflow_id", "workflow"),
            ("task_id", "task"),
            ("acceptance_record_id", "future-record"),
            ("artifact_snapshot_id", "snapshot"),
            ("artifact_digest", artifact),
            ("acceptance_policy_id", "m6-acceptance"),
            ("acceptance_policy_version", "1"),
            ("acceptance_policy_bundle_sha", policy),
        ];
        for (k, v) in bound {
            assign(&mut record, k, json::s(v));
        }
        assign(&mut record, "state_revision", Value::Int(0));
        assign(&mut record, "issued_at", json::s(EPOCH));
        assign(&mut record, "expires_at", json::s("2026-11-19T12:05:00Z"));
        let mut criteria = record.get("criteria").unwrap().as_arr().unwrap()[1..].to_vec();
        let mut evaluations = envelope.get("evaluations").unwrap().as_arr().unwrap()[1..].to_vec();
        for (i, (criterion, evaluation)) in criteria.iter_mut().zip(&mut evaluations).enumerate() {
            let name = if i == 0 { "docs" } else { "review" };
            let evaluator = if i == 0 { IDS[0] } else { IDS[3] };
            assign(criterion, "criterion_id", json::s(name));
            assign(criterion, "evaluator_id", json::s(evaluator));
            assign(criterion, "fresh_until", json::s("2026-11-19T12:04:00Z"));
            assign(evaluation, "criterion_id", json::s(name));
            assign(evaluation, "evaluator_id", json::s(evaluator));
            assign(
                evaluation,
                "registry_revision",
                Value::Int(if i == 0 { 1 } else { 4 }),
            );
        }
        assign(&mut record, "criteria", Value::Arr(criteria));
        let digest = json::digest(&record.without("record_digest")).unwrap();
        assign(&mut record, "record_digest", json::s(digest.as_str()));
        assign(&mut envelope, "record", record);
        assign(&mut envelope, "evaluations", Value::Arr(evaluations));
        let mut documents = BTreeMap::new();
        let mut contributions = Vec::new();
        let members = match case {
            Case::One => 1,
            Case::Failure | Case::Omit => 3,
            _ => 2,
        };
        for (i, id) in IDS.iter().take(members).enumerate() {
            let mut att = json::parse(include_bytes!(
                "../../../../docs/contracts/proposed/examples/runner-attestation.valid.json"
            ))
            .unwrap()
            .without("command_id")
            .without("command_digest");
            for (k, v) in bound {
                assign(&mut att, k, json::s(v));
            }
            let evidence_id = format!("member-{i}");
            for (k, v) in [
                ("attestation_id", evidence_id.as_str()),
                ("contract_version", "runner-attestation/2"),
                ("attempt_id", "attempt-1"),
                ("verdict", if i == 2 { "fail" } else { "pass" }),
                ("criterion_id", "docs"),
                ("evaluator_id", *id),
                ("canonical_target", "workflow:m6-lab/offline-proof/workflow"),
                ("started_at", EPOCH),
                ("finished_at", EPOCH),
                ("fresh_until", "2026-11-19T12:04:00Z"),
            ] {
                assign(&mut att, k, json::s(v));
            }
            assign(
                &mut att,
                "input_digests",
                Value::Arr(vec![json::s(artifact)]),
            );
            assign(&mut att, "state_revision", Value::Int(0));
            assign(&mut att, "registry_revision", Value::Int(i as i64 + 1));
            let auth = edit(
                att.get("auth").unwrap(),
                "key_id",
                json::s(&format!("key-{i}")),
            );
            assign(&mut att, "auth", auth);
            documents.insert(evidence_id.clone(), signed(att, &material(i)));
            if case != Case::Omit || i != 2 {
                contributions.push(json::obj([
                    ("attestation_id", json::s(&evidence_id)),
                    ("criterion_id", json::s("docs")),
                    ("evaluator_id", json::s(id)),
                ]));
            }
        }
        if !matches!(case, Case::Legacy | Case::LegacyDenied) {
            assign(
                &mut envelope,
                "contract_version",
                json::s("authenticated-acceptance-record/3"),
            );
            assign(&mut envelope, "attempt_id", json::s("attempt-1"));
            assign(&mut envelope, "contributions", Value::Arr(contributions));
        }
        let record = signed(envelope, &RECORD_KEY);
        let attempt = Attempt {
            record: json::digest(&record).unwrap(),
            members: documents
                .iter()
                .map(|(id, value)| (id.clone(), json::digest(value).unwrap()))
                .collect(),
        };
        Self {
            policy: policy.into(),
            artifact: artifact.into(),
            record,
            documents,
            attempt,
        }
    }
}
