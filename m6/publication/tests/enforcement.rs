//! Current owner evidence and commit-boundary refusal in the isolated publication profile.
#![allow(clippy::unwrap_used, clippy::panic)]

#[allow(dead_code)]
#[path = "../examples/support/mod.rs"]
mod support;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use support::{intent, scope, Disposable, FixtureAuthority, FixtureSigner, ALLOW, APPROVAL};
use writ_broker::clock::{ClockError, TrustedClock};
use writ_kernel::{
    json::{self, Value},
    time::Timestamp,
};
use writ_policy_publication_lab::{
    Approval, CurrentTrust, Error, FixtureEvidence, Intent, Operation, Registry, TrustAuthority,
    VerificationKey, VerificationSource,
};

struct Authority {
    inner: FixtureAuthority,
    removed: AtomicBool,
    approvals: AtomicUsize,
    remove_at: AtomicUsize,
}

struct Guard<'a, T>(&'a Authority, T);

impl TrustAuthority for &Authority {
    fn current(&self) -> Result<impl CurrentTrust + '_, Error> {
        Ok(Guard(self, self.inner.current()?))
    }
}

impl<T: CurrentTrust> TrustedClock for Guard<'_, T> {
    fn now(&self) -> Result<Timestamp, ClockError> {
        self.1.now()
    }
}

impl<T: CurrentTrust> VerificationSource for Guard<'_, T> {
    fn resolve(&self, id: &str) -> Result<VerificationKey, Error> {
        self.1.resolve(id)
    }
}

impl<T: CurrentTrust> CurrentTrust for Guard<'_, T> {
    fn enforcement_evidence(&self) -> Option<FixtureEvidence> {
        FixtureEvidence::scenario(if self.0.removed.load(Ordering::SeqCst) {
            "removed"
        } else {
            "current"
        })
        .unwrap()
    }
    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        at: Timestamp,
    ) -> Result<Approval, Error> {
        let result = self.1.approve(intent, presentation, at)?;
        if self.0.approvals.fetch_add(1, Ordering::SeqCst) + 1
            == self.0.remove_at.load(Ordering::SeqCst)
        {
            self.0.removed.store(true, Ordering::SeqCst);
        }
        Ok(result)
    }
}

#[test]
fn lost_enforcement_registration_at_final_owner_check_rolls_back() {
    let temp = Disposable::create().unwrap();
    let (inner, signer) = FixtureSigner::pair();
    let auth = Authority {
        inner,
        removed: AtomicBool::new(false),
        approvals: AtomicUsize::new(0),
        remove_at: AtomicUsize::new(usize::MAX),
    };
    let registry = Registry::create(&temp.path().join("policy.sqlite"), scope(), &auth).unwrap();
    let permit = signer.publication(ALLOW, 1);
    let first = intent(Operation::Publish, 0, 1, &permit);
    registry
        .publish(&first, &permit, &auth.inner.authorize(&first, "first"))
        .unwrap();
    let read = || registry.enforcement_labels(&scope(), 0, 1).unwrap();
    let rule = |view: Value| {
        view.get("enforcement_labels")
            .unwrap()
            .as_arr()
            .unwrap()
            .iter()
            .find(|row| row.get("subject") == Some(&json::s("m6.synthetic.permit")))
            .unwrap()
            .clone()
    };
    let current = rule(read());
    assert_eq!(current.get("label"), Some(&json::s("ENFORCED")));
    assert_eq!(
        current.get("policy"),
        Some(&json::s(permit.fingerprint().unwrap().as_str()))
    );
    let approval = signer.publication(APPROVAL, 2);
    registry.preview(1, 2, Some(&approval)).unwrap();
    let second = intent(Operation::Publish, 1, 2, &approval);
    let proof = auth.inner.authorize(&second, "second");
    let history = registry.audit().unwrap();
    auth.remove_at
        .store(auth.approvals.load(Ordering::SeqCst) + 2, Ordering::SeqCst);
    assert_eq!(
        registry.publish(&second, &approval, &proof),
        Err(Error::InvalidPolicy)
    );
    assert_eq!(registry.audit().unwrap(), history);
    let view = read();
    assert_eq!(view.get("selection_revision"), Some(&Value::Int(1)));
    let degraded = rule(view);
    assert_eq!(degraded.get("label"), Some(&json::s("UNVERIFIED")));
    assert_eq!(degraded.get("enforcement_point"), Some(&Value::Null));
    assert_eq!(
        degraded.get("reasons"),
        Some(&Value::Arr(vec![json::s("ENFORCEMENT_POINT_MISSING")]))
    );
    auth.removed.store(false, Ordering::SeqCst);
    registry.publish(&second, &approval, &proof).unwrap();
    assert_eq!(registry.audit().unwrap().len(), 2);
    let later = registry.enforcement_labels(&scope(), 1, 1).unwrap();
    let page = later.get("history").unwrap();
    assert_eq!(page.get("events").unwrap().as_arr().unwrap().len(), 1);
    assert_eq!(page.get("next"), Some(&Value::Null));
    assert!(later
        .get("enforcement_labels")
        .unwrap()
        .as_arr()
        .unwrap()
        .iter()
        .any(
            |r| r.get("subject") == Some(&json::s("m6.synthetic.approval"))
                && r.get("label") == Some(&json::s("APPROVAL REQUIRED"))
        ));
    auth.inner.revoke(support::KEY_AUTHORIZATION);
    assert_eq!(rule(read()).get("label"), Some(&json::s("UNVERIFIED")));
    assert_eq!(
        rule(read()).get("reasons"),
        Some(&Value::Arr(vec![json::s("POLICY_KEY_REVOKED")]))
    );
    assert_eq!(registry.current_selection(), Err(Error::Trust));
}

#[test]
fn every_registered_rule_runs_through_the_selected_permit_consuming_effect() {
    use writ_broker::{effects::EffectAdapter, execute::Executed, refusal::Refusal};
    use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};
    let profiles: &[(&[u8], &[u8])] = &[
        (
            ALLOW,
            include_bytes!("../../compiler/fixtures/permit.cases.json"),
        ),
        (
            APPROVAL,
            include_bytes!("../../compiler/fixtures/approval.cases.json"),
        ),
        (
            support::DENY,
            include_bytes!("../../compiler/fixtures/deny.cases.json"),
        ),
        (
            include_bytes!("../../compiler/fixtures/optional-inspect.input.json"),
            include_bytes!("../../compiler/fixtures/optional-inspect.cases.json"),
        ),
    ];
    for (draft, cases) in profiles {
        let dir = Disposable::create().unwrap();
        let (auth, signer) = FixtureSigner::pair();
        let registry =
            Registry::create(&dir.path().join("registry"), scope(), auth.clone()).unwrap();
        let publication = signer.publication(draft, 1);
        let command = intent(Operation::Publish, 0, 1, &publication);
        registry
            .publish(
                &command,
                &publication,
                &auth.authorize(&command, "activate"),
            )
            .unwrap();
        let ledger = SqliteLedger::create(&dir.path().join("ledger")).unwrap();
        let effects =
            SqliteEffectStore::create(&dir.path().join("effects"), ledger.ledger_id()).unwrap();
        let broker = support::LabBroker::new(&registry, ledger, effects);
        let mut completed = 0;
        let cases = json::parse(cases).unwrap();
        for case in cases.as_arr().unwrap() {
            let expected = case.get("expected").unwrap();
            if expected
                .get("matched_rule_ids")
                .unwrap()
                .as_arr()
                .unwrap()
                .is_empty()
            {
                continue;
            }
            let request = support::seal_request(
                &case
                    .get("request")
                    .unwrap()
                    .without("expected_state_revision"),
            );
            let (outcome, permit) = broker.execute(&request, None).unwrap();
            match expected.get("status").unwrap().as_str().unwrap() {
                "allow" => {
                    assert_eq!(outcome, Ok(Executed::EffectCompleted));
                    let permit = permit.unwrap();
                    assert!(broker.effects.evidence(permit.id()).unwrap().is_some());
                    assert_eq!(
                        broker.present(&permit, request.request()),
                        Ok(Refusal::PermitReplayed)
                    );
                    completed += 1;
                }
                "require_approval" => {
                    assert_eq!(outcome, Err(Refusal::ApprovalRequired));
                    assert!(permit.is_none());
                }
                "deny" => {
                    assert_eq!(
                        outcome.unwrap_err().code(),
                        expected.get("reason_code").unwrap().as_str().unwrap()
                    );
                    assert!(permit.is_none());
                }
                _ => panic!("unrecognized sealed fixture result"),
            }
            let count: i64 = rusqlite::Connection::open(dir.path().join("effects"))
                .unwrap()
                .query_row("SELECT count(*) FROM effects", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, completed);
        }
    }
}
