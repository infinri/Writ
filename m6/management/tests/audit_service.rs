#![allow(clippy::unwrap_used)]

use std::{fs, path::PathBuf};

use rusqlite::{params, Connection};
use writ_kernel::json::{self, Map, Value};
use writ_policy_management_lab::{
    audit::{AuditOperation, AuditReader, REPLICA_SCHEMA},
    Error, Scope,
};

struct Fixture {
    path: PathBuf,
    db: Connection,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let path =
            std::env::temp_dir().join(format!("writ-audit-{name}-{}.db", std::process::id()));
        let db = Connection::open(&path).unwrap();
        db.execute_batch(REPLICA_SCHEMA).unwrap();
        Self { path, db }
    }

    fn add(
        &self,
        scope: (&str, &str),
        n: i64,
        id: &str,
        kind: &str,
        status: &str,
        payload: &Value,
    ) {
        self.db
            .execute(
                "INSERT INTO audit_events VALUES (?1,?2,?3,?4,?5,'2026-11-05T12:00:00Z',
            'trace-1','req-1','agent:fixture','file.write',?6,'1',?7)",
                params![
                    scope.0,
                    scope.1,
                    n,
                    id,
                    kind,
                    status,
                    json::canonical(payload).unwrap()
                ],
            )
            .unwrap();
    }

    fn query(&self, op: AuditOperation, fields: Value) -> Result<Value, Error> {
        AuditReader::new(&self.path).query(
            &Scope {
                tenant: "tenant".into(),
                project: "project".into(),
            },
            op,
            fields.as_obj().unwrap().clone(),
        )
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[test]
fn golden_explanations_retain_deciding_rules_and_missing_conditions() {
    let fixture = Fixture::new("golden");
    let golden = json::parse(include_bytes!(
        "../../../docs/contracts/fixtures/decisions/m0-golden.json"
    ))
    .unwrap();
    let cases = golden.get("cases").unwrap().as_arr().unwrap();
    assert_eq!(cases.len(), 26);
    for (index, case) in cases.iter().enumerate() {
        let id = case.get("case_id").unwrap().as_str().unwrap();
        let expected = case.get("expected").unwrap();
        let status = expected.get("status").unwrap().as_str().unwrap();
        let mut payload = expected.as_obj().unwrap().clone();
        payload.insert("raw_request".into(), json::s("SECRET-CANARY"));
        fixture.add(
            ("tenant", "project"),
            index as i64 + 1,
            id,
            "decision",
            status,
            &Value::Obj(payload),
        );
        let shown = fixture
            .query(
                AuditOperation::Explain,
                json::obj([("event_id", json::s(id))]),
            )
            .unwrap();
        assert_eq!(shown.get("status"), Some(&json::s(status)));
        let explanation = shown.get("explanation").unwrap();
        if expected
            .get("matched_rule_ids")
            .unwrap()
            .as_arr()
            .unwrap()
            .is_empty()
        {
            assert!(explanation
                .get("condition")
                .and_then(Value::as_str)
                .is_some());
        }
        for key in ["reason_code", "matched_rule_ids"] {
            assert_eq!(explanation.get(key), expected.get(key));
        }
        assert!(!json::canonical(&shown).unwrap().contains("SECRET-CANARY"));
    }
}

#[test]
fn timeline_uses_scoped_causal_sequence_and_only_a_receipt_establishes_completion() {
    let fixture = Fixture::new("timeline");
    let records = [
        ("adapter", "accepted"),
        ("decision", "allow"),
        ("approval", "approved"),
        ("effect.intent", "reserved"),
        ("effect.dispatch", "dispatched"),
        ("effect.interrupted", "dispatched"),
        ("effect.receipt", "observed_failure"),
        ("effect.reconcile", "observed_success"),
        ("effect.refusal", "refused"),
    ];
    for (index, (kind, status)) in records.iter().enumerate().rev() {
        fixture.add(
            ("tenant", "project"),
            index as i64 + 1,
            &format!("evt-{index}"),
            kind,
            status,
            &json::obj([("reason_code", json::s("RECORDED_REASON"))]),
        );
    }
    for scope in [("other", "project"), ("tenant", "other")] {
        fixture.add(
            scope,
            1,
            "absent",
            "log",
            "error",
            &json::obj([("message", json::s("SECRET-CANARY"))]),
        );
    }
    let page = |after| {
        fixture
            .query(
                AuditOperation::Timeline,
                json::obj([
                    ("after", Value::Int(after)),
                    ("request_id", json::s("req-1")),
                    ("limit", Value::Int(5)),
                ]),
            )
            .unwrap()
    };
    let first = page(0);
    assert_eq!(first.get("next"), Some(&Value::Int(5)));
    let second = page(5);
    assert_eq!(second.get("next"), Some(&Value::Null));
    let events: Vec<_> = [first, second]
        .into_iter()
        .flat_map(|p| p.get("events").unwrap().as_arr().unwrap().to_vec())
        .collect();
    assert_eq!(events.len(), 9);
    for (i, event) in events.iter().enumerate() {
        assert_eq!(event.get("sequence"), Some(&Value::Int(i as i64 + 1)));
    }
    for (event, (state, executed)) in events.iter().skip(3).zip([
        ("INTENT", Value::Bool(false)),
        ("DISPATCHED", Value::Null),
        ("UNKNOWN_RECONCILE", Value::Null),
        ("FAILED", Value::Null),
        ("COMPLETED", Value::Bool(true)),
        ("REFUSED", Value::Bool(false)),
    ]) {
        let evidence = event.get("explanation").unwrap();
        assert_eq!(evidence.get("effect_state"), Some(&json::s(state)));
        assert_eq!(evidence.get("executed"), Some(&executed));
    }
    assert_eq!(
        fixture.query(
            AuditOperation::Explain,
            json::obj([("event_id", json::s("absent"))])
        ),
        Err(Error::NotFound)
    );
    let selected = fixture
        .query(
            AuditOperation::Search,
            json::obj([
                ("status", json::s("reserved")),
                ("policy_version", json::s("1")),
            ]),
        )
        .unwrap();
    assert_eq!(selected.get("events").unwrap().as_arr().unwrap().len(), 1);
    assert_eq!(
        fixture.query(AuditOperation::Timeline, Value::Obj(Map::new())),
        Err(Error::InvalidRequest)
    );
    assert_eq!(
        fixture.query(
            AuditOperation::Search,
            json::obj([("limit", Value::Int(501))])
        ),
        Err(Error::InvalidRequest)
    );
    fixture.add(
        ("tenant", "project"),
        10,
        "bad",
        "effect.dispatch",
        "observed_success",
        &Value::Obj(Map::new()),
    );
    assert_eq!(
        fixture.query(
            AuditOperation::Explain,
            json::obj([("event_id", json::s("bad"))])
        ),
        Err(Error::Unavailable)
    );
}

#[test]
fn failures_and_unavailability_only_report_recorded_metadata() {
    let fixture = Fixture::new("failure");
    let evidence = json::obj([
        ("reason_code", json::s("TRUST_UNAVAILABLE")),
        ("component", json::s("kernel")),
        ("dependency", json::s("trust-owner")),
        ("recovery_code", json::s("RESTORE_TRUST")),
        ("receipt_id", json::s("receipt-1")),
        ("decision_id", json::s("decision-1")),
        ("grant_id", json::s("grant-1")),
        ("permit_id", json::s("permit-1")),
        ("attempt_id", json::s("attempt-1")),
        ("model_explanation", json::s("SECRET-CANARY")),
        ("stack_trace", json::s("SECRET-CANARY")),
        ("safe_message", json::s("SECRET-CANARY")),
    ]);
    for (i, (kind, status)) in [("failure", "error"), ("decision", "unavailable")]
        .into_iter()
        .enumerate()
    {
        fixture.add(
            ("tenant", "project"),
            i as i64 + 1,
            kind,
            kind,
            status,
            &evidence,
        );
        let event = fixture
            .query(
                AuditOperation::Explain,
                json::obj([("event_id", json::s(kind))]),
            )
            .unwrap();
        let result = event.get("explanation").unwrap();
        for key in [
            "reason_code",
            "component",
            "dependency",
            "recovery_code",
            "receipt_id",
            "decision_id",
            "grant_id",
            "permit_id",
            "attempt_id",
        ] {
            assert_eq!(result.get(key), evidence.get(key));
        }
        assert!(!json::canonical(&event).unwrap().contains("SECRET-CANARY"));
    }
    let missing = fixture.path.with_extension("missing");
    let failure = AuditReader::new(&missing).query(
        &Scope {
            tenant: "tenant".into(),
            project: "project".into(),
        },
        AuditOperation::Search,
        Map::new(),
    );
    assert_eq!(failure, Err(Error::Unavailable));
    assert!(!missing.exists());
}

#[cfg(feature = "fixtures")]
#[test]
fn accepted_owner_mutation_is_searchable_and_explainable_by_receipt() {
    use writ_policy_management_lab::{execute_v1_with_service, fixture, Service, Store};
    let path = std::env::temp_dir().join(format!("writ-owner-audit-{}.db", std::process::id()));
    let store = Store::create(&path).unwrap();
    let authority = fixture::authority();
    let reader = AuditReader::draft_owner(&store);
    let service = Service::new(&store, Some(&authority)).with_audit(&reader);
    let call = |credential: &[u8], operation: &str, project: &str, fields: Value| {
        let mut fields = fields.as_obj().unwrap().clone();
        fields.insert(
            "scope".into(),
            json::obj([("tenant", json::s("m6-lab")), ("project", json::s(project))]),
        );
        fields.insert("version".into(), json::s("/manage/v1"));
        fields.insert("operation".into(), json::s(operation));
        let body = json::canonical(&Value::Obj(fields)).unwrap();
        json::parse(&execute_v1_with_service(
            &service,
            credential,
            body.as_bytes(),
        ))
        .unwrap()
    };
    let candidate =
        json::parse(include_bytes!("../../compiler/fixtures/permit.input.json")).unwrap();
    let mutation = |preview| {
        json::obj([
            ("id", json::s("lab")),
            ("expected_revision", Value::Int(0)),
            ("idempotency_key", json::s("do-not-disclose")),
            ("preview", Value::Bool(preview)),
            ("candidate", candidate.clone()),
        ])
    };
    assert_eq!(
        call(b"fixture-author", "create", "offline-proof", mutation(true)).get("ok"),
        Some(&Value::Bool(true))
    );
    let search = || {
        call(
            b"fixture-viewer",
            "audit.search",
            "offline-proof",
            json::obj([("action_kind", json::s("draft.create"))]),
        )
    };
    assert_eq!(
        search().get("result").unwrap().get("events"),
        Some(&Value::Arr(vec![]))
    );
    let created = call(
        b"fixture-author",
        "create",
        "offline-proof",
        mutation(false),
    );
    let audit_id = created
        .get("result")
        .unwrap()
        .get("audit_id")
        .unwrap()
        .as_int()
        .unwrap();
    let result = search();
    let events = result
        .get("result")
        .unwrap()
        .get("events")
        .unwrap()
        .as_arr()
        .unwrap();
    assert_eq!(events.len(), 1);
    let event = &events[0];
    let event_id = event.get("event_id").unwrap();
    assert_eq!(event_id, &json::s(&format!("draft-{audit_id}")));
    assert_eq!(event.get("principal_id"), Some(&json::s("fixture-author")));
    assert_eq!(event.get("action_kind"), Some(&json::s("draft.create")));
    assert!(event.get("request_id").is_none());
    let explained = call(
        b"fixture-viewer",
        "audit.explain",
        "offline-proof",
        json::obj([("event_id", event_id.clone())]),
    );
    assert_eq!(explained.get("result"), Some(event));
    let evidence = event.get("explanation").unwrap();
    assert_eq!(evidence.get("source_audit_id"), Some(&Value::Int(audit_id)));
    assert_eq!(evidence.get("state_revision"), Some(&Value::Int(1)));
    assert_eq!(
        evidence.get("content_digest"),
        Some(&json::s(json::digest(&candidate).unwrap().as_str()))
    );
    let secret_free = json::canonical(event).unwrap();
    assert!(!secret_free.contains("do-not-disclose"));
    assert!(!secret_free.contains("candidate"));
    assert_eq!(
        call(
            b"fixture-viewer",
            "audit.explain",
            "other",
            json::obj([("event_id", event_id.clone())])
        )
        .get("ok"),
        Some(&Value::Bool(false))
    );
    fs::remove_file(path).unwrap();
}
