#![allow(clippy::unwrap_used)]

use std::{
    cell::Cell,
    fs,
    sync::{mpsc, RwLock, RwLockReadGuard, TryLockError},
    thread,
};

use writ_kernel::json::{self, Map, Value};
use writ_policy_management_lab::{
    error_response, execute_v1_with_service, Authenticator, Error, EvaluatorBoundary,
    EvaluatorOperation, Grant, LifecycleCall, LifecycleKind, PinnedPrincipal, Principal, Realm,
    Result, Role, Scope, Service, Store, WaiverBoundary, WaiverOperation,
};

struct Authority(Cell<Role>);

struct CheckedPrincipal<'a> {
    principal: Principal,
    role: &'a Cell<Role>,
}

impl PinnedPrincipal for CheckedPrincipal<'_> {
    fn principal(&self) -> &Principal {
        &self.principal
    }

    fn still_valid(&self) -> bool {
        self.principal.grants[0].role == self.role.get()
    }
}

impl Authenticator for Authority {
    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        Some(Box::new(CheckedPrincipal {
            principal: self.authenticate(credential)?,
            role: &self.0,
        }))
    }

    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        let (realm, subject) = match credential {
            b"human" => (Realm::Management, "human"),
            b"other" => (Realm::Management, "other"),
            b"service" => (Realm::ManagementService, "service"),
            b"agent" => (Realm::Agent, "agent"),
            _ => return None,
        };
        Some(Principal {
            realm,
            subject: subject.into(),
            grants: vec![Grant {
                scope: Scope {
                    tenant: "t1".into(),
                    project: "p1".into(),
                },
                role: self.0.get(),
            }],
        })
    }
}

struct Boundary<'a> {
    authority: &'a Authority,
    calls: Cell<usize>,
}

impl EvaluatorBoundary for Boundary<'_> {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: EvaluatorOperation,
        fields: Map,
    ) -> Result<Value> {
        self.calls.set(self.calls.get() + 1);
        match operation {
            EvaluatorOperation::Revoke => {
                let binding = Value::Obj(fields);
                if !call.reserve(
                    LifecycleKind::Evaluator,
                    "dispatch",
                    &binding,
                    &json::s("owner-id"),
                )? {
                    return Err(Error::Unavailable);
                }
                Ok(json::s("new"))
            }
            EvaluatorOperation::Recover => call.correlation(LifecycleKind::Evaluator, "dispatch"),
            _ => Err(Error::InvalidRequest),
        }
    }
}

impl WaiverBoundary for Boundary<'_> {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: WaiverOperation,
        _: Map,
    ) -> Result<Value> {
        assert_eq!(operation, WaiverOperation::Begin);
        self.calls.set(self.calls.get() + 1);
        self.authority.0.set(Role::Viewer);
        (call.reauthorize)()?;
        Ok(Value::Null)
    }
}

#[test]
fn lifecycle_scope_role_restart_and_commit_reauthorization() {
    let path = std::env::temp_dir().join(format!(
        "writ-management-lifecycle-{}.sqlite",
        std::process::id()
    ));
    let store = Store::create(&path).unwrap();
    let authority = Authority(Cell::new(Role::Publisher));
    let boundary = Boundary {
        authority: &authority,
        calls: Cell::new(0),
    };
    let service = Service::new(&store, Some(&authority))
        .with_evaluator(&boundary)
        .with_waiver(&boundary);
    let publish = br#"{"operation":"evaluator_revoke","version":"/manage/v1","scope":{"tenant":"t1","project":"p1"},"evaluator_id":"doc-check","expected_revision":0,"idempotency_key":"dispatch","intent_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","presentation":"opaque"}"#;
    let other_scope = br#"{"operation":"evaluator_revoke","version":"/manage/v1","scope":{"tenant":"t1","project":"p2"},"evaluator_id":"doc-check","expected_revision":0,"idempotency_key":"dispatch","intent_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","presentation":"opaque"}"#;
    let recover = br#"{"operation":"evaluator_recover","version":"/manage/v1","scope":{"tenant":"t1","project":"p1"},"idempotency_key":"dispatch"}"#;
    let begin = br#"{"operation":"waiver_begin","version":"/manage/v1","scope":{"tenant":"t1","project":"p1"},"waiver_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111","transition_ref":"transition","approver_issuer":"realm","approver_subject":"approver","idempotency_key":"ceremony"}"#;
    for (credential, request, error) in [
        (b"service".as_slice(), publish.as_slice(), Error::NotFound),
        (b"agent".as_slice(), publish.as_slice(), Error::Unauthorized),
        (b"human".as_slice(), other_scope.as_slice(), Error::NotFound),
        (b"human".as_slice(), begin.as_slice(), Error::NotFound),
    ] {
        assert_eq!(
            execute_v1_with_service(&service, credential, request),
            error_response(error)
        );
    }
    let unsafe_begin = String::from_utf8(begin.to_vec()).unwrap().replace(
        r#""transition_ref":"transition""#,
        r#""transition_ref":"transition","acceptance_record":{}"#,
    );
    assert!(writ_policy_management_lab::RequestV1::parse(unsafe_begin.as_bytes()).is_err());
    assert_eq!(boundary.calls.get(), 0);
    let write = execute_v1_with_service(&service, b"human", publish);
    assert_eq!(
        json::parse(&write).unwrap().get("result"),
        Some(&json::s("new"))
    );
    assert_eq!(
        execute_v1_with_service(&service, b"human", publish),
        error_response(Error::Unavailable)
    );
    let changed = br#"{"operation":"evaluator_revoke","version":"/manage/v1","scope":{"tenant":"t1","project":"p1"},"evaluator_id":"doc-check","expected_revision":1,"idempotency_key":"dispatch","intent_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","presentation":"opaque"}"#;
    assert_eq!(
        execute_v1_with_service(&service, b"human", changed),
        error_response(Error::IdempotencyConflict)
    );
    assert_eq!(
        execute_v1_with_service(&service, b"other", recover),
        error_response(Error::NotFound)
    );
    let reopened = Store::open(&path).unwrap();
    let service = Service::new(&reopened, Some(&authority)).with_evaluator(&boundary);
    let recovered = execute_v1_with_service(&service, b"human", recover);
    assert_eq!(
        json::parse(&recovered).unwrap().get("result"),
        Some(&json::s("owner-id"))
    );
    assert_eq!(
        execute_v1_with_service(&service, b"human", begin),
        error_response(Error::NotFound)
    );
    authority.0.set(Role::WaiverOperator);
    assert_eq!(
        execute_v1_with_service(&service, b"human", begin),
        error_response(Error::Unavailable)
    );
    let service = service.with_waiver(&boundary);
    assert_eq!(
        execute_v1_with_service(&service, b"human", begin),
        error_response(Error::Unauthorized)
    );
    assert_eq!(authority.0.get(), Role::Viewer);
    fs::remove_file(path).unwrap();
}

struct LockedAuthority(RwLock<Role>);

struct LockedPrincipal<'a> {
    principal: Principal,
    _role: RwLockReadGuard<'a, Role>,
}

impl PinnedPrincipal for LockedPrincipal<'_> {
    fn principal(&self) -> &Principal {
        &self.principal
    }
}

impl Authenticator for LockedAuthority {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        Authority(Cell::new(*self.0.read().ok()?)).authenticate(credential)
    }

    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        let role = self.0.read().ok()?;
        let principal = Authority(Cell::new(*role)).authenticate(credential)?;
        Some(Box::new(LockedPrincipal {
            principal,
            _role: role,
        }))
    }
}

struct CommitBoundary {
    checked: mpsc::SyncSender<()>,
    resume: mpsc::Receiver<()>,
}

impl EvaluatorBoundary for CommitBoundary {
    fn execute(&self, call: &LifecycleCall<'_>, _: EvaluatorOperation, _: Map) -> Result<Value> {
        (call.reauthorize)()?;
        self.checked.send(()).unwrap();
        self.resume.recv().unwrap();
        Ok(json::s("committed"))
    }
}

#[test]
fn grant_revocation_orders_after_the_owner_commit() {
    let path = std::env::temp_dir().join(format!(
        "writ-management-authority-pin-{}.sqlite",
        std::process::id()
    ));
    let store = Store::create(&path).unwrap();
    let authority = LockedAuthority(RwLock::new(Role::Publisher));
    let wire = br#"{"operation":"evaluator_revoke","version":"/manage/v1","scope":{"tenant":"t1","project":"p1"},"evaluator_id":"doc-check","expected_revision":0,"idempotency_key":"dispatch","intent_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","presentation":"opaque"}"#;
    let (checked, at_commit) = mpsc::sync_channel(0);
    let (release, resume) = mpsc::sync_channel(0);
    thread::scope(|threads| {
        let owner = threads.spawn(|| {
            let boundary = CommitBoundary { checked, resume };
            let service = Service::new(&store, Some(&authority)).with_evaluator(&boundary);
            execute_v1_with_service(&service, b"human", wire)
        });
        at_commit.recv().unwrap();
        let revocation_blocked = matches!(authority.0.try_write(), Err(TryLockError::WouldBlock));
        release.send(()).unwrap();
        let response = owner.join().unwrap();
        assert!(revocation_blocked);
        assert_eq!(
            json::parse(&response).unwrap().get("result"),
            Some(&json::s("committed"))
        );
    });
    *authority.0.write().unwrap() = Role::Viewer;
    assert_eq!(
        execute_v1_with_service(&Service::new(&store, Some(&authority)), b"human", wire),
        error_response(Error::NotFound)
    );
    fs::remove_file(path).unwrap();
}
