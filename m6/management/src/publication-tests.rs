#![allow(clippy::unwrap_used)]

use std::{
    cell::{Cell, RefCell},
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

use writ_kernel::time::Timestamp;
use writ_policy_publication_lab::{Scope as RegistryScope, SignedPolicy};

use super::*;
use crate::{Change, Mutation};

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Boundary {
    scope: RegistryScope,
    audit: RefCell<Vec<Audit>>,
    calls: Cell<usize>,
    commit: Cell<bool>,
    retirement: Option<Publication>,
}

impl PublicationBoundary for Boundary {
    fn scope(&self) -> &RegistryScope {
        &self.scope
    }

    fn preview(
        &self,
        expected_revision: i64,
        _: i64,
        candidate: Option<&Publication>,
    ) -> std::result::Result<Publication, RegistryError> {
        if expected_revision != self.audit.borrow().last().map_or(0, |event| event.revision) {
            return Err(RegistryError::Conflict);
        }
        candidate.cloned().ok_or(RegistryError::Format)
    }

    fn preview_retire(
        &self,
        _: i64,
        _: i64,
        _: &str,
    ) -> std::result::Result<Publication, RegistryError> {
        self.retirement.clone().ok_or(RegistryError::Format)
    }

    fn change(
        &self,
        intent: &Intent,
        _: Option<&Publication>,
        _: &[u8],
        reauthorize: &dyn Fn() -> Result<()>,
    ) -> std::result::Result<Commit, RegistryError> {
        self.calls.set(self.calls.get() + 1);
        reauthorize().map_err(|_| RegistryError::Authority)?;
        if intent.operation() == Operation::Retire {
            return Err(RegistryError::ActiveDependents);
        }
        if self.commit.get() {
            let value = intent.to_value();
            let binding = value.get("management").unwrap();
            let event = Audit {
                sequence: 1,
                revision: 1,
                operation: intent.operation(),
                publication_version: intent.publication_version(),
                publication_digest: value
                    .get("publication_digest")
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .into(),
                intent_digest: intent.fingerprint().unwrap().to_string(),
                management_digest: Some(binding.get("digest").unwrap().as_str().unwrap().into()),
                human: binding.get("publisher").unwrap().as_str().unwrap().into(),
                ceremony: "disposable".into(),
                at: Timestamp::parse_rfc3339("2026-11-19T12:00:00Z").unwrap(),
            };
            self.audit.borrow_mut().push(event);
        }
        Err(RegistryError::Storage)
    }

    fn audit(&self) -> std::result::Result<Vec<Audit>, RegistryError> {
        Ok(self.audit.borrow().clone())
    }

    fn current_selection(&self) -> std::result::Result<Option<Commit>, RegistryError> {
        Ok(self.audit.borrow().last().map(event_commit))
    }
}

struct Scenario {
    scratch: Scratch,
    scope: Scope,
    boundary: Boundary,
    candidate: Publication,
}

impl Scenario {
    fn new(commit: bool) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "writ-managed-pub-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        let scratch = Scratch(path);
        let scope = Scope {
            tenant: "m6-lab".into(),
            project: "offline-proof".into(),
        };
        let draft = json::obj([("fixture", Value::Bool(true))]);
        let store = Store::create(&scratch.0.join("drafts")).unwrap();
        store
            .mutate(
                &scope,
                "publisher",
                Mutation {
                    id: "demo".into(),
                    expected_revision: 0,
                    key: "draft-create".into(),
                    preview: false,
                    change: Change::Create(draft.clone()),
                },
                b"fixture-create",
                || Ok(()),
            )
            .unwrap();
        let boundary = Boundary {
            scope: RegistryScope::new(&scope.tenant, &scope.project).unwrap(),
            audit: RefCell::default(),
            calls: Cell::default(),
            commit: Cell::new(commit),
            retirement: None,
        };
        let signed = SignedPolicy {
            payload: Value::Null,
            envelope: Value::Null,
        };
        let candidate = Publication {
            authorization: signed.clone(),
            acceptance: signed,
            prerequisites: json::obj([
                ("draft", draft),
                (
                    "proof_digest",
                    json::s(Digest::of_bytes(b"fixture-proof").as_str()),
                ),
            ]),
        };
        Self {
            scratch,
            scope,
            boundary,
            candidate,
        }
    }

    fn store(&self) -> Store {
        Store::open(&self.scratch.0.join("drafts")).unwrap()
    }

    fn execute(
        &self,
        key: &str,
        submission: Option<(&str, &[u8])>,
        allow_overlap: bool,
        acknowledge_self: bool,
    ) -> Result<Value> {
        let draft = DraftRef {
            id: "demo".into(),
            revision: 1,
        };
        let candidate = self.candidate.clone();
        let key = key.into();
        let request = match submission {
            Some((digest, presentation)) => PublicationRequest::Publish {
                draft,
                expected_revision: 0,
                publication_version: 1,
                candidate,
                acknowledge_self,
                key,
                intent_digest: digest.into(),
                presentation: presentation.into(),
            },
            None => PublicationRequest::PreviewPublish {
                draft,
                expected_revision: 0,
                publication_version: 1,
                candidate,
                acknowledge_self,
                key,
            },
        };
        execute(
            &self.store(),
            &self.boundary,
            &self.scope,
            "publisher",
            request,
            allow_overlap,
            || Ok(()),
        )
    }

    fn submit(&self, key: &str, digest: &str, presentation: &[u8]) -> Result<Value> {
        self.execute(key, Some((digest, presentation)), true, true)
    }

    fn preview(&self, key: &str) -> Value {
        self.execute(key, None, true, true).unwrap()
    }
}

fn intent(preview: &Value) -> &str {
    preview.get("intent_digest").unwrap().as_str().unwrap()
}

#[test]
fn uncertain_reply_recovers_only_exact_registry_receipt_after_reopen() {
    let scenario = Scenario::new(true);
    let preview = scenario.preview("publish");
    let digest = intent(&preview);
    assert_ne!(digest, intent(&scenario.preview("different-key")));
    assert!(!has_table(&scenario.store().connection(false).unwrap()).unwrap());
    assert_eq!(
        scenario.submit("publish", digest, b"human"),
        Err(Error::Unavailable)
    );
    let receipt = scenario.submit("publish", digest, b"human").unwrap();
    assert_eq!(receipt.get("audit_id").and_then(Value::as_int), Some(1));
    assert_eq!(
        receipt.get("intent_digest").and_then(Value::as_str),
        Some(digest)
    );
    assert_eq!(
        scenario.submit("publish", digest, b"changed"),
        Err(Error::IdempotencyConflict)
    );
    assert_eq!(scenario.boundary.calls.get(), 1);
    let history = history(
        &scenario.store(),
        &scenario.boundary,
        &scenario.scope,
        0,
        50,
    )
    .unwrap();
    let events = history.get("events").and_then(Value::as_arr).unwrap();
    assert_eq!(events[0].get("context"), preview.get("context"));
}

#[test]
fn dependent_added_after_preflight_replays_definitive_conflict_without_dispatch() {
    let mut scenario = Scenario::new(true);
    let preview = scenario.preview("publish");
    assert_eq!(
        scenario.submit("publish", intent(&preview), b"human"),
        Err(Error::Unavailable)
    );
    scenario.boundary.retirement = Some(scenario.candidate.clone());
    let route = |request| {
        execute(
            &scenario.store(),
            &scenario.boundary,
            &scenario.scope,
            "publisher",
            request,
            true,
            || Ok(()),
        )
    };
    let preview = route(PublicationRequest::PreviewRetire {
        expected_revision: 2,
        publication_version: 1,
        acknowledge_self: true,
        key: "retire".into(),
        confirmation: "retire 1".into(),
    })
    .unwrap();
    let before = scenario.boundary.audit().unwrap();
    for _ in 0..2 {
        assert_eq!(
            route(PublicationRequest::Retire {
                expected_revision: 2,
                publication_version: 1,
                acknowledge_self: true,
                key: "retire".into(),
                confirmation: "retire 1".into(),
                intent_digest: intent(&preview).into(),
                presentation: b"human".to_vec(),
            }),
            Err(Error::RevisionConflict)
        );
    }
    assert_eq!(scenario.boundary.calls.get(), 2);
    assert_eq!(scenario.boundary.audit().unwrap(), before);
}

#[test]
fn unresolved_intent_is_never_dispatched_twice() {
    let scenario = Scenario::new(false);
    let preview = scenario.preview("lost");
    assert_eq!(
        scenario.submit("lost", intent(&preview), b"human"),
        Err(Error::Unavailable)
    );
    scenario.boundary.commit.set(true);
    assert_eq!(
        scenario.submit("lost", intent(&preview), b"human"),
        Err(Error::Unavailable)
    );
    assert!(scenario.boundary.audit().unwrap().is_empty());
    let new_preview = scenario.preview("fresh-key");
    assert_eq!(
        scenario.submit("fresh-key", intent(&new_preview), b"fresh"),
        Err(Error::Unavailable)
    );
    assert!(scenario
        .submit("fresh-key", intent(&new_preview), b"fresh")
        .is_ok());
    assert_eq!(
        scenario.submit("lost", intent(&preview), b"human"),
        Err(Error::Unavailable)
    );
    assert_eq!(scenario.boundary.calls.get(), 2);
}

#[test]
fn management_intent_write_failure_refuses_before_registry_call() {
    let scenario = Scenario::new(true);
    let preview = scenario.preview("publish");
    let store = scenario.store();
    let db = store.connection(true).unwrap();
    db.execute_batch(SCHEMA).unwrap();
    db.execute_batch(
        "CREATE TRIGGER fail_publication_intent BEFORE INSERT ON publication_intents
         BEGIN SELECT RAISE(ABORT, 'fixture intent storage failure'); END;",
    )
    .unwrap();
    assert_eq!(
        scenario.submit("publish", intent(&preview), b"human"),
        Err(Error::Unavailable)
    );
    assert_eq!(scenario.boundary.calls.get(), 0);
    assert!(scenario.boundary.audit().unwrap().is_empty());
}

#[test]
fn overlap_requires_host_permission_and_ack_with_shared_idempotency_namespace() {
    let scenario = Scenario::new(true);
    assert_eq!(
        scenario.execute("publish", None, false, true),
        Err(Error::Unauthorized)
    );
    assert_eq!(
        scenario.execute("publish", None, true, false),
        Err(Error::Unauthorized)
    );
    let preview = scenario.preview("draft-create");
    assert_eq!(
        preview
            .get("context")
            .unwrap()
            .get("self_publication_acknowledged"),
        Some(&Value::Bool(true))
    );
    assert_eq!(
        scenario.submit("draft-create", intent(&preview), b"human"),
        Err(Error::IdempotencyConflict)
    );
    assert_eq!(scenario.boundary.calls.get(), 0);
}
