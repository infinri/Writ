//! Authenticated acceptance records, runner attestations and use-time checks
//! (D-036 item 1). Test keys only.

use super::*;
use crate::keys::{AcceptanceRecordKey, MemoryKeys, RunnerAttestationKey};
use writ_kernel::json::parse;

pub(crate) const RECORD_KEY: [u8; 32] = [0x5a; 32];
pub(crate) const RUNNER_KEY: [u8; 32] = [0x6b; 32];

fn sha(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

fn set(v: &Value, path: &[&str], new: Value) -> Value {
    let mut out = v.clone();
    let mut cur = &mut out;
    for (i, k) in path.iter().enumerate() {
        let Value::Obj(m) = cur else {
            panic!("not an object")
        };
        if i + 1 == path.len() {
            m.insert((*k).to_string(), new);
            return out;
        }
        cur = m.get_mut(*k).unwrap();
    }
    out
}

fn record_keys() -> MemoryKeys<AcceptanceRecordKey> {
    let mut k = MemoryKeys::new();
    k.register(
        "acc-key-1",
        AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
    );
    k
}

fn runner_keys() -> MemoryKeys<RunnerAttestationKey> {
    let mut k = MemoryKeys::new();
    k.register(
        "runner-key-1",
        RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
    );
    k
}

/// The kernel record (criteria `build` mechanical by `ci-runner`, `docs`
/// evidence by `doc-check`), with a consistent `record_digest`.
fn kernel_record() -> Value {
    let text = format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"acc-1","tenant_id":"t1","project_id":"p1","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{b}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{c}","state_revision":7,"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}},{{"criterion_id":"docs","mandatory":true,"required_class":"evidence","status":"pass","evaluator_id":"doc-check","evaluator_trust":"E","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{z}"}}"#,
        b = sha('b'),
        c = sha('c'),
        z = sha('0')
    );
    reseal(parse(text.as_bytes()).unwrap())
}

fn reseal(mut record: Value) -> Value {
    let d = json::digest(&record.without("record_digest")).unwrap();
    if let Value::Obj(m) = &mut record {
        m.insert("record_digest".into(), json::s(d.as_str()));
    }
    record
}

fn evaluation(criterion: &str, evaluator: &str, attestation: Option<&str>) -> Value {
    json::obj([
        ("attestation_id", attestation.map_or(Value::Null, json::s)),
        ("criterion_id", json::s(criterion)),
        ("environment_digest", json::s(&sha('2'))),
        ("evaluation_id", json::s(&format!("eval-{criterion}"))),
        ("evaluator_id", json::s(evaluator)),
        ("evaluator_version", json::s("1.4.0")),
        ("evidence_digests", Value::Arr(vec![])),
        ("implementation_digest", json::s(&sha('1'))),
        ("registry_revision", Value::Int(12)),
    ])
}

/// An unsigned envelope around `record`.
fn envelope(record: Value) -> Value {
    json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s("acc-key-1")),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        ),
        ("contract_version", json::s(RECORD_CONTRACT)),
        (
            "evaluations",
            Value::Arr(vec![
                evaluation("build", "ci-runner", Some("att-1")),
                evaluation("docs", "doc-check", None),
            ]),
        ),
        ("issuer", json::s("acceptance-service")),
        ("mode", json::s("governed")),
        ("purpose", json::s(RECORD_PURPOSE)),
        ("record", record),
    ])
}

/// Sign `doc` with `key` (canonical bytes without `auth.mac`).
pub(crate) fn sign(doc: &Value, key: &[u8]) -> Value {
    let tag = mac::sign_hex(key, mac_bytes(doc).unwrap().as_bytes()).unwrap();
    set(doc, &["auth", "mac"], json::s(&tag))
}

pub(crate) fn good_record() -> Value {
    sign(&envelope(kernel_record()), &RECORD_KEY)
}

fn attestation() -> Value {
    let text =
        include_str!("../../../docs/contracts/proposed/examples/runner-attestation.valid.json");
    sign(&parse(text.as_bytes()).unwrap(), &RUNNER_KEY)
}

fn registry_entry(evaluator: &str) -> RegistryEntry {
    let text = include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    );
    let mut v = parse(text.as_bytes()).unwrap();
    if evaluator == "doc-check" {
        v = set(&v, &["evaluator_id"], json::s("doc-check"));
        v = set(&v, &["class"], json::s("evidence"));
        v = set(&v, &["trust"], json::s("E"));
        v = set(&v, &["criteria"], Value::Arr(vec![json::s("docs")]));
        if let Value::Obj(m) = &mut v {
            m.remove("command");
        }
    }
    RegistryEntry::validate(&v).unwrap()
}

fn current() -> CurrentState {
    let text = format!(
        r#"{{"tenant_id":"t1","project_id":"p1","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":7,"now":"2026-11-07T11:00:00Z","consumed_record_ids":[],"revoked_evaluator_ids":[]}}"#,
        sha('b'),
        sha('c')
    );
    CurrentState::from_value(&parse(text.as_bytes()).unwrap()).unwrap()
}

fn requirements(extra: &str) -> AcceptanceRequirements {
    let text = format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}}{extra}]}}"#,
        sha('c')
    );
    AcceptanceRequirements::from_value(&parse(text.as_bytes()).unwrap()).unwrap()
}

#[test]
fn a_signed_record_verifies_and_projects_to_the_kernel_contract() {
    let v = verify_record(&good_record(), &record_keys()).unwrap();
    assert_eq!(v.record().acceptance_record_id, "acc-1");
    assert_eq!(v.issuer(), "acceptance-service");
    assert_eq!(v.key_id(), "acc-key-1");
    assert_eq!(v.attestation_ids(), ["att-1"]);
    assert_eq!(
        AcceptanceRecord::from_value(good_record().get("record").unwrap()).as_ref(),
        Ok(v.record()),
        "the projection is the record member unchanged"
    );
    // The MAC input is the canonical document without auth.mac only.
    let bytes = mac_bytes(&good_record()).unwrap();
    assert!(bytes.contains(r#""auth":{"alg":"hmac-sha256","key_id":"acc-key-1"}"#));
    assert!(!bytes.contains(r#""mac""#));
    assert!(bytes.contains(RECORD_PURPOSE) && bytes.contains("acceptance-service"));
}

#[test]
fn every_authenticated_member_is_covered_by_the_mac() {
    let good = good_record();
    let edits: Vec<(&[&str], Value)> = vec![
        (&["issuer"], json::s("other-service")),
        (&["mode"], json::s("advisory")),
        (&["auth", "key_id"], json::s("acc-key-2")),
        (&["record", "tenant_id"], json::s("t2")),
        (&["record", "artifact_digest"], json::s(&sha('d'))),
        (&["record", "state_revision"], Value::Int(8)),
        (&["record", "expires_at"], json::s("2026-11-08T12:00:00Z")),
        (&["record", "transition", "to_state"], json::s("released")),
        (&["record", "acceptance_record_id"], json::s("acc-2")),
    ];
    let mut keys = record_keys();
    keys.register(
        "acc-key-2",
        AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
    );
    for (path, value) in edits {
        let tampered = set(&good, path, value);
        assert_eq!(
            verify_record(&tampered, &keys).err(),
            Some(AcceptanceCode::RecordUnauthenticated),
            "{path:?}"
        );
    }
    // Evaluation bindings and evidence are authenticated too.
    let mut evals = good.get("evaluations").unwrap().as_arr().unwrap().to_vec();
    evals[0] = set(&evals[0], &["registry_revision"], Value::Int(13));
    let tampered = set(&good, &["evaluations"], Value::Arr(evals.clone()));
    assert_eq!(
        verify_record(&tampered, &keys).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
    evals[0] = set(
        &evals[0],
        &["evidence_digests"],
        Value::Arr(vec![json::s(&sha('e'))]),
    );
    let tampered = set(&good, &["evaluations"], Value::Arr(evals));
    assert_eq!(
        verify_record(&tampered, &keys).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
}

#[test]
fn unknown_revoked_unavailable_and_wrong_purpose_keys_fail_closed() {
    let good = good_record();
    let mut keys = record_keys();
    keys.revoke("acc-key-1");
    assert_eq!(
        verify_record(&good, &keys).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
    assert_eq!(
        verify_record(&good, &MemoryKeys::<AcceptanceRecordKey>::new()).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
    let mut down = record_keys();
    down.set_unavailable(true);
    assert_eq!(
        verify_record(&good, &down).err(),
        Some(AcceptanceCode::KeyUnavailable)
    );
    // Signed with the runner key: not an acceptance-record key.
    let runner_signed = sign(&envelope(kernel_record()), &RUNNER_KEY);
    assert_eq!(
        verify_record(&runner_signed, &record_keys()).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
    // Another purpose or algorithm.
    let other = sign(
        &set(
            &envelope(kernel_record()),
            &["purpose"],
            json::s(ATTESTATION_PURPOSE),
        ),
        &RECORD_KEY,
    );
    assert_eq!(
        verify_record(&other, &record_keys()).err(),
        Some(AcceptanceCode::RecordMalformed)
    );
    let alg = sign(
        &set(
            &envelope(kernel_record()),
            &["auth", "alg"],
            json::s("hmac-sha1"),
        ),
        &RECORD_KEY,
    );
    assert_eq!(
        verify_record(&alg, &record_keys()).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
}

#[test]
fn malformed_envelopes_and_bindings_are_refused() {
    let e = envelope(kernel_record());
    let evals = |items: Vec<Value>| set(&e, &["evaluations"], Value::Arr(items));
    let cases = vec![
        ("extra member", set(&e, &["extra"], Value::Null)),
        (
            "contract",
            set(
                &e,
                &["contract_version"],
                json::s("authenticated-acceptance-record/2"),
            ),
        ),
        ("mode", set(&e, &["mode"], json::s("shadow"))),
        ("record", set(&e, &["record", "status"], json::s("great"))),
        (
            "missing criterion",
            evals(vec![evaluation("build", "ci-runner", Some("att-1"))]),
        ),
        (
            "wrong evaluator",
            evals(vec![
                evaluation("build", "other", Some("att-1")),
                evaluation("docs", "doc-check", None),
            ]),
        ),
        (
            "mechanical without attestation",
            evals(vec![
                evaluation("build", "ci-runner", None),
                evaluation("docs", "doc-check", None),
            ]),
        ),
        (
            "evidence with attestation",
            evals(vec![
                evaluation("build", "ci-runner", Some("att-1")),
                evaluation("docs", "doc-check", Some("att-2")),
            ]),
        ),
        (
            "duplicate",
            evals(vec![
                evaluation("build", "ci-runner", Some("att-1")),
                evaluation("build", "ci-runner", Some("att-1")),
            ]),
        ),
    ];
    for (name, doc) in cases {
        assert_eq!(
            verify_record(&sign(&doc, &RECORD_KEY), &record_keys()).err(),
            Some(AcceptanceCode::RecordMalformed),
            "{name}"
        );
    }
    // An unsigned (or wrongly signed) malformed record says nothing about its content.
    let unsigned = set(&e, &["record", "status"], json::s("great"));
    assert_eq!(
        verify_record(&unsigned, &record_keys()).err(),
        Some(AcceptanceCode::RecordUnauthenticated)
    );
}

#[test]
fn an_advisory_record_never_satisfies_a_protected_transition() {
    let doc = sign(
        &set(&envelope(kernel_record()), &["mode"], json::s("advisory")),
        &RECORD_KEY,
    );
    assert_eq!(
        verify_record(&doc, &record_keys()).err(),
        Some(AcceptanceCode::Advisory)
    );
}

#[test]
fn attestations_verify_with_the_runner_key_only() {
    let att = attestation();
    let v = verify_attestation(&att, &runner_keys(), now()).unwrap();
    assert_eq!(v.attestation_id(), "att-1");
    let tampered = set(&att, &["exit_status"], Value::Int(1));
    assert_eq!(
        verify_attestation(&tampered, &runner_keys(), now()).err(),
        Some(AcceptanceCode::AttestationUnauthenticated)
    );
    let mut revoked = runner_keys();
    revoked.revoke("runner-key-1");
    assert_eq!(
        verify_attestation(&att, &revoked, now()).err(),
        Some(AcceptanceCode::AttestationUnauthenticated)
    );
    let mut down = runner_keys();
    down.set_unavailable(true);
    assert_eq!(
        verify_attestation(&att, &down, now()).err(),
        Some(AcceptanceCode::AttestationKeyUnavailable)
    );
    let record_signed = sign(
        &parse(
            include_str!("../../../docs/contracts/proposed/examples/runner-attestation.valid.json")
                .as_bytes(),
        )
        .unwrap(),
        &RECORD_KEY,
    );
    assert_eq!(
        verify_attestation(&record_signed, &runner_keys(), now()).err(),
        Some(AcceptanceCode::AttestationUnauthenticated)
    );
    let malformed = sign(
        &set(&att, &["purpose"], json::s(RECORD_PURPOSE)),
        &RUNNER_KEY,
    );
    assert_eq!(
        verify_attestation(&malformed, &runner_keys(), now()).err(),
        Some(AcceptanceCode::AttestationMalformed)
    );
}

struct Use {
    current: CurrentState,
    requirements: AcceptanceRequirements,
    entries: Vec<RegistryEntry>,
    attestations: Vec<VerifiedAttestation>,
    consumed: BTreeSet<String>,
}

impl Use {
    fn new() -> Use {
        Use {
            current: current(),
            requirements: requirements(""),
            entries: vec![registry_entry("ci-runner"), registry_entry("doc-check")],
            attestations: vec![verify_attestation(&attestation(), &runner_keys(), now()).unwrap()],
            consumed: BTreeSet::new(),
        }
    }

    fn run(&self, record: &Value) -> Result<Vec<String>, AcceptanceCode> {
        let verified = verify_record(record, &record_keys())?;
        let entries = self.entries.clone();
        let registry = move |id: &str| {
            entries
                .iter()
                .find(|e| e.as_value().get("evaluator_id").and_then(Value::as_str) == Some(id))
                .cloned()
        };
        check_use(
            &verified,
            &UseContext {
                current: &self.current,
                requirements: &self.requirements,
                registry: &registry,
                attestations: &self.attestations,
                consumed_attestations: &self.consumed,
            },
        )
    }
}

#[test]
fn use_time_checks_accept_a_current_bound_record_and_name_its_attestations() {
    assert_eq!(
        Use::new().run(&good_record()),
        Ok(vec!["att-1".to_string()])
    );
}

#[test]
fn the_kernel_check_runs_first_and_human_criteria_are_unavailable() {
    let mut u = Use::new();
    u.current.artifact_digest = sha('d');
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::Kernel(AcceptanceRefusal::ArtifactMismatch))
    );
    let mut u = Use::new();
    u.current.consumed_record_ids.push("acc-1".into());
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::Kernel(AcceptanceRefusal::Replayed))
    );
    let mut u = Use::new();
    u.current.now = Timestamp::parse_strict("2026-11-07T12:00:00Z").unwrap();
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::Kernel(AcceptanceRefusal::Expired))
    );
    // A required human criterion: the record would need human signoff (M4).
    let mut criteria = kernel_record()
        .get("criteria")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    criteria.push(parse(br#"{"criterion_id":"review","mandatory":true,"required_class":"human","status":"pass","evaluator_id":"alice","evaluator_trust":"H","fresh_until":"2026-11-07T11:30:00Z"}"#).unwrap());
    let record = reseal(set(&kernel_record(), &["criteria"], Value::Arr(criteria)));
    let mut env = envelope(record);
    let mut evals = env.get("evaluations").unwrap().as_arr().unwrap().to_vec();
    evals.push(evaluation("review", "alice", None));
    env = set(&env, &["evaluations"], Value::Arr(evals));
    let mut u = Use::new();
    u.requirements = requirements(r#",{"criterion_id":"review","required_class":"human"}"#);
    assert_eq!(
        u.run(&sign(&env, &RECORD_KEY)),
        Err(AcceptanceCode::HumanUnavailable)
    );
}

#[test]
fn the_registry_binding_is_checked_at_use_time() {
    let mut u = Use::new();
    u.entries
        .retain(|e| e.as_value().get("evaluator_id").and_then(Value::as_str) != Some("doc-check"));
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::EvaluatorUnregistered)
    );
    let base = registry_entry("ci-runner").as_value().clone();
    let revoked = set(
        &set(&base, &["status"], json::s("revoked")),
        &["revoked_at"],
        json::s("2026-11-07T10:30:00Z"),
    );
    let edits: Vec<(Value, AcceptanceCode)> = vec![
        (revoked, AcceptanceCode::EvaluatorRevoked),
        (
            set(&base, &["scope", "project_id"], json::s("p2")),
            AcceptanceCode::EvaluatorBindingMismatch,
        ),
        (
            set(&base, &["evaluator_version"], json::s("1.5.0")),
            AcceptanceCode::EvaluatorBindingMismatch,
        ),
        (
            set(&base, &["implementation_digest"], json::s(&sha('9'))),
            AcceptanceCode::EvaluatorBindingMismatch,
        ),
        (
            set(&base, &["registry_revision"], Value::Int(13)),
            AcceptanceCode::EvaluatorBindingMismatch,
        ),
        (
            set(&base, &["criteria"], Value::Arr(vec![json::s("lint")])),
            AcceptanceCode::EvaluatorBindingMismatch,
        ),
    ];
    for (entry, want) in edits {
        let mut u = Use::new();
        u.entries[0] = RegistryEntry::validate(&entry).unwrap();
        assert_eq!(u.run(&good_record()), Err(want), "{entry:?}");
    }
}

#[test]
fn the_attestation_must_bind_exactly_this_promotion() {
    let mut u = Use::new();
    u.attestations.clear();
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::AttestationUnknown)
    );
    let mut u = Use::new();
    u.consumed.insert("att-1".into());
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::AttestationReplayed)
    );
    let base = parse(
        include_str!("../../../docs/contracts/proposed/examples/runner-attestation.valid.json")
            .as_bytes(),
    )
    .unwrap();
    let edits: Vec<(&[&str], Value, AcceptanceCode)> = vec![
        (
            &["acceptance_record_id"],
            json::s("acc-2"),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["criterion_id"],
            json::s("lint"),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["state_revision"],
            Value::Int(6),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["artifact_digest"],
            json::s(&sha('d')),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["command_digest"],
            json::s(&sha('9')),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["evaluator_version"],
            json::s("1.3.0"),
            AcceptanceCode::AttestationBindingMismatch,
        ),
        (
            &["exit_status"],
            Value::Int(1),
            AcceptanceCode::AttestationFailed,
        ),
        (
            &["fresh_until"],
            json::s("2026-11-07T11:00:00Z"),
            AcceptanceCode::AttestationStale,
        ),
    ];
    for (path, value, want) in edits {
        let mut u = Use::new();
        let doc = sign(&set(&base, path, value), &RUNNER_KEY);
        u.attestations = vec![verify_attestation(&doc, &runner_keys(), now()).unwrap()];
        assert_eq!(u.run(&good_record()), Err(want), "{path:?}");
    }
    // Signed with another (valid) runner key than the registry entry names.
    let mut keys = runner_keys();
    keys.register(
        "runner-key-2",
        RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
    );
    let doc = sign(
        &set(&base, &["auth", "key_id"], json::s("runner-key-2")),
        &RUNNER_KEY,
    );
    let mut u = Use::new();
    u.attestations = vec![verify_attestation(&doc, &keys, now()).unwrap()];
    assert_eq!(
        u.run(&good_record()),
        Err(AcceptanceCode::AttestationBindingMismatch)
    );
}
