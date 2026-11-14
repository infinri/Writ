//! Authenticated test evidence (D-037 item 3; m3/closure/bindings.md "Evidence"):
//! registry entries registered through production administration, fixture inline records
//! translated field by field into `authenticated-acceptance-record/1` (the fixture record
//! is the `record` member unchanged, never repaired) and MACed by the test acceptance
//! source, and synthetic runner attestations for the record's mechanical criteria, MACed by
//! the test runner key. The test keys and sources do not complete production key
//! provisioning, the acceptance service or the runner (M4..M6). HMAC is a MAC, not a
//! signature.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use writ_broker::acceptance::{
    mac_bytes, ATTESTATION_PURPOSE, MAC_ALG, RECORD_CONTRACT, RECORD_PURPOSE,
};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, RunnerAttestationKey};
use writ_broker::workflow::{AcceptanceRecordSource, AttestationSource, SourceUnavailable};
use writ_broker::writ_kernel::json::{self, Digest, Value};

use crate::common::{text, with, Labels, TEST_EVIDENCE};

/// Test acceptance-record key (purpose `writ.acceptance-record.v1`).
pub const RECORD_KEY_ID: &str = "m3c-acceptance-key-1";
const RECORD_KEY: [u8; 32] = [0x3c; 32];
/// Test runner attestation key (purpose `writ.runner-attestation.v1`); the id is the one the
/// registry entries name.
pub const RUNNER_KEY_ID: &str = "runner-key-1";
const RUNNER_KEY: [u8; 32] = [0x4d; 32];
/// The test acceptance service identity.
pub const RECORD_ISSUER: &str = "m3c-test-acceptance-service";

fn sha(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

// ---------------------------------------------------------------------------
// HMAC-SHA256 from the kernel's SHA-256 (no new dependency).

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .filter_map(|i| {
            text.get(i..i + 2)
                .and_then(|b| u8::from_str_radix(b, 16).ok())
        })
        .collect()
}

fn hmac_hex(key: &[u8], message: &[u8]) -> String {
    let mut k = key.to_vec();
    k.resize(64, 0);
    let pad = |b: u8| k.iter().map(|x| x ^ b).collect::<Vec<u8>>();
    let mut inner = pad(0x36);
    inner.extend_from_slice(message);
    let inner = unhex(Digest::of_bytes(&inner).hex());
    let mut outer = pad(0x5c);
    outer.extend_from_slice(&inner);
    Digest::of_bytes(&outer).hex().to_string()
}

/// `doc` with `auth.mac` set to the MAC of its canonical bytes without `auth.mac`.
fn sign(doc: &Value, key: &[u8]) -> Option<Value> {
    let tag = hmac_hex(key, mac_bytes(doc)?.as_bytes());
    Some(with(doc, &["auth", "mac"], json::s(&tag)))
}

pub fn record_keys() -> MemoryKeys<AcceptanceRecordKey> {
    let mut k = MemoryKeys::new();
    if let Ok(key) = AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()) {
        k.register(RECORD_KEY_ID, key);
    }
    k
}

pub fn runner_keys() -> MemoryKeys<RunnerAttestationKey> {
    let mut k = MemoryKeys::new();
    if let Ok(key) = RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()) {
        k.register(RUNNER_KEY_ID, key);
    }
    k
}

// ---------------------------------------------------------------------------
// The test registry: the four evaluators the fixture records name.

struct Evaluator {
    id: &'static str,
    class: &'static str,
    trust: &'static str,
    criterion: &'static str,
    command: bool,
}

const EVALUATORS: [Evaluator; 4] = [
    Evaluator {
        id: "ci-runner",
        class: "mechanical",
        trust: "M",
        criterion: "build",
        command: true,
    },
    Evaluator {
        id: "evidence-engine",
        class: "evidence",
        trust: "E",
        criterion: "citations",
        command: false,
    },
    Evaluator {
        id: "approval-service",
        class: "human",
        trust: "H",
        criterion: "signoff",
        command: false,
    },
    Evaluator {
        id: "reviewer-model",
        class: "model_assisted",
        trust: "MA",
        criterion: "style",
        command: false,
    },
];
const EVALUATOR_VERSION: &str = "1.4.0";
const REGISTRY_REVISION: i64 = 12;
const COMMAND_ID: &str = "cargo-test";

/// The `evaluator-registry-entry/1` of every test evaluator, scoped to the tenant and project.
pub fn registry_entries(tenant: &str, project: &str) -> Vec<Value> {
    EVALUATORS
        .iter()
        .map(|e| {
            let mut m = json::Map::new();
            let mut put = |k: &str, v: Value| {
                m.insert(k.to_string(), v);
            };
            put("contract_version", json::s("evaluator-registry-entry/1"));
            put("evaluator_id", json::s(e.id));
            put("evaluator_version", json::s(EVALUATOR_VERSION));
            put("class", json::s(e.class));
            put("trust", json::s(e.trust));
            put("implementation_digest", json::s(&sha('1')));
            put("environment_digest", json::s(&sha('2')));
            if e.command {
                put(
                    "command",
                    json::obj([
                        ("command_digest", json::s(&sha('3'))),
                        ("command_id", json::s(COMMAND_ID)),
                    ]),
                );
            }
            put(
                "attestation",
                json::obj([
                    ("alg", json::s(MAC_ALG)),
                    ("key_id", json::s(RUNNER_KEY_ID)),
                ]),
            );
            put("criteria", Value::Arr(vec![json::s(e.criterion)]));
            put(
                "scope",
                json::obj([
                    ("project_id", json::s(project)),
                    ("tenant_id", json::s(tenant)),
                ]),
            );
            put("owner", json::s("m3-closure"));
            put("publisher", json::s("m3-closure-harness"));
            put("published_at", json::s("2026-11-01T09:00:00Z"));
            put("registry_revision", Value::Int(REGISTRY_REVISION));
            put("status", json::s("active"));
            Value::Obj(m)
        })
        .collect()
}

fn evaluator(id: &str) -> Option<&'static Evaluator> {
    EVALUATORS.iter().find(|e| e.id == id)
}

fn attestation_id(record_id: &str, criterion_id: &str) -> String {
    format!("att-{record_id}-{criterion_id}")
}

/// The authenticated envelope of a fixture inline record (an object): the record member is
/// the fixture record unchanged; one evaluation per record criterion naming its evaluator,
/// the registered evaluator binding and, for trust `M` only, its attestation id. A
/// criterion whose evaluator is not registered gets no evaluation (the envelope then fails
/// verification as malformed, as the acceptance service could not bind it).
pub fn authenticated_record(record: &Value) -> Option<Value> {
    let record_id = text(record, "acceptance_record_id").unwrap_or_default();
    let mut evaluations = Vec::new();
    for c in record
        .get("criteria")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
    {
        let (Some(criterion), Some(evaluator_id)) =
            (text(c, "criterion_id"), text(c, "evaluator_id"))
        else {
            continue;
        };
        let Some(_) = evaluator(&evaluator_id) else {
            continue;
        };
        let attestation = if c.get("evaluator_trust").and_then(Value::as_str) == Some("M") {
            json::s(&attestation_id(&record_id, &criterion))
        } else {
            Value::Null
        };
        evaluations.push(json::obj([
            ("attestation_id", attestation),
            ("criterion_id", json::s(&criterion)),
            ("environment_digest", json::s(&sha('2'))),
            (
                "evaluation_id",
                json::s(&format!("eval-{record_id}-{criterion}")),
            ),
            ("evaluator_id", json::s(&evaluator_id)),
            ("evaluator_version", json::s(EVALUATOR_VERSION)),
            ("evidence_digests", Value::Arr(Vec::new())),
            ("implementation_digest", json::s(&sha('1'))),
            ("registry_revision", Value::Int(REGISTRY_REVISION)),
        ]));
    }
    let doc = json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s(RECORD_KEY_ID)),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        ),
        ("contract_version", json::s(RECORD_CONTRACT)),
        ("evaluations", Value::Arr(evaluations)),
        ("issuer", json::s(RECORD_ISSUER)),
        ("mode", json::s("governed")),
        ("purpose", json::s(RECORD_PURPOSE)),
        ("record", record.clone()),
    ]);
    sign(&doc, &RECORD_KEY)
}

/// Synthetic runner attestations for the record's trust-`M` criteria. They are bound to the
/// record's context fields (the binding under test), but the run outcome (exit status, run
/// times, freshness) is the declared harness runner outcome `runner` (cases.json), never
/// read from the record's own criterion status or times. A criterion whose record lacks a
/// needed binding field gets none.
pub fn attestations(record: &Value, runner: &Value) -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let s = |k: &str| record.get(k).filter(|v| v.as_str().is_some()).cloned();
    let record_id = text(record, "acceptance_record_id").unwrap_or_default();
    for c in record
        .get("criteria")
        .and_then(Value::as_arr)
        .unwrap_or(&[])
    {
        if c.get("evaluator_trust").and_then(Value::as_str) != Some("M") {
            continue;
        }
        let (Some(criterion), Some(evaluator_id)) =
            (text(c, "criterion_id"), text(c, "evaluator_id"))
        else {
            continue;
        };
        let target = match (
            text(record, "tenant_id"),
            text(record, "project_id"),
            text(record, "workflow_id"),
        ) {
            (Some(t), Some(p), Some(w)) => format!("workflow:{t}/{p}/{w}"),
            _ => continue,
        };
        let fields = [
            "tenant_id",
            "project_id",
            "task_id",
            "workflow_id",
            "artifact_snapshot_id",
            "artifact_digest",
            "acceptance_policy_id",
            "acceptance_policy_version",
            "acceptance_policy_bundle_sha",
        ];
        let Some(values) = fields.iter().map(|k| s(k)).collect::<Option<Vec<Value>>>() else {
            continue;
        };
        let outcome = |k: &str| runner.get(k).cloned();
        let (
            Some(transition),
            Some(revision),
            Some(exit),
            Some(started),
            Some(finished),
            Some(fresh),
        ) = (
            record.get("transition").cloned(),
            record.get("state_revision").cloned(),
            outcome("exit_status"),
            outcome("started_at"),
            outcome("finished_at"),
            outcome("fresh_until"),
        )
        else {
            continue;
        };
        let id = attestation_id(&record_id, &criterion);
        let mut m = json::Map::new();
        for (k, v) in fields.iter().zip(values.iter()) {
            m.insert((*k).to_string(), v.clone());
        }
        let mut put = |k: &str, v: Value| {
            m.insert(k.to_string(), v);
        };
        put("contract_version", json::s("runner-attestation/1"));
        put("purpose", json::s(ATTESTATION_PURPOSE));
        put("attestation_id", json::s(&id));
        put("transition", transition);
        put("canonical_target", json::s(&target));
        put("state_revision", revision);
        put("acceptance_record_id", json::s(&record_id));
        put("criterion_id", json::s(&criterion));
        put("evaluator_id", json::s(&evaluator_id));
        put("evaluator_version", json::s(EVALUATOR_VERSION));
        put("implementation_digest", json::s(&sha('1')));
        put("environment_digest", json::s(&sha('2')));
        put("registry_revision", Value::Int(REGISTRY_REVISION));
        put("command_id", json::s(COMMAND_ID));
        put("command_digest", json::s(&sha('3')));
        put("input_digests", Value::Arr(vec![values[5].clone()]));
        put("evidence_digests", Value::Arr(Vec::new()));
        put("exit_status", exit);
        put("output_digest", json::s(&sha('4')));
        put("started_at", started);
        put("finished_at", finished);
        put("fresh_until", fresh);
        put(
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s(RUNNER_KEY_ID)),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        );
        if let Some(signed) = sign(&Value::Obj(m), &RUNNER_KEY) {
            out.push((id, signed));
        }
    }
    out
}

/// The trusted test sources of records and attestations, by id (the fixture records are
/// keyed by their own `acceptance_record_id`; the source does not filter by scope, so
/// production's own scope checks are exercised, bindings.md).
pub struct Docs {
    records: Mutex<BTreeMap<String, Value>>,
    attestations: Mutex<BTreeMap<String, Value>>,
    labels: Arc<Labels>,
}

impl Docs {
    pub fn new(labels: Arc<Labels>) -> Docs {
        Docs {
            records: Mutex::new(BTreeMap::new()),
            attestations: Mutex::new(BTreeMap::new()),
            labels,
        }
    }

    /// Issue the authenticated translation of a fixture inline record (an object with a
    /// string `acceptance_record_id`; anything else issues nothing) and the attestations
    /// of the declared runner outcome. Called once per record id at setup.
    pub fn issue(&self, record: &Value, runner: &Value) {
        let Some(id) = text(record, "acceptance_record_id") else {
            return;
        };
        let Some(doc) = authenticated_record(record) else {
            return;
        };
        if let Ok(mut r) = self.records.lock() {
            r.insert(id, doc);
        }
        if let Ok(mut a) = self.attestations.lock() {
            for (id, doc) in attestations(record, runner) {
                a.insert(id, doc);
            }
        }
    }
}

impl AcceptanceRecordSource for Docs {
    fn record(&self, _t: &str, _p: &str, id: &str) -> Result<Option<Value>, SourceUnavailable> {
        let doc = self
            .records
            .lock()
            .map_err(|_| SourceUnavailable)?
            .get(id)
            .cloned();
        if doc.is_some() {
            self.labels.add(TEST_EVIDENCE);
        }
        Ok(doc)
    }
}

impl AttestationSource for Docs {
    fn attestation(
        &self,
        _t: &str,
        _p: &str,
        id: &str,
    ) -> Result<Option<Value>, SourceUnavailable> {
        Ok(self
            .attestations
            .lock()
            .map_err(|_| SourceUnavailable)?
            .get(id)
            .cloned())
    }
}
