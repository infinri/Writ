//! Run the D-051 lifecycle with disposable SQLite stores and synthetic authorities.

#[allow(clippy::unwrap_used, dead_code)]
mod support;

use support::*;
use writ_broker::execute::Executed;
use writ_ledger_sqlite::{SqliteEffectStore, SqliteLedger};
use writ_policy_publication_lab::{Error, Operation, Registry};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let directory = Disposable::create()?;
    let path = directory.path().join("policies.sqlite");
    let (authority, signer) = FixtureSigner::pair();
    let registry = Registry::create(&path, scope(), authority.clone())?;
    let allow = signer.publication(ALLOW, 1);
    let deny = signer.publication(DENY, 2);

    let publish = intent(Operation::Publish, 0, 1, &allow);
    let first = registry.publish(
        &publish,
        &allow,
        &authority.authorize(&publish, "publish-one"),
    )?;
    let ledger = SqliteLedger::create(&directory.path().join("ledger.sqlite"))
        .map_err(|_| Error::Storage)?;
    let effects =
        SqliteEffectStore::create(&directory.path().join("effects.sqlite"), ledger.ledger_id())
            .map_err(|_| Error::Storage)?;
    let broker = LabBroker::new(&registry, ledger, effects);
    let initial = request("demo-allow");
    let (result, permit) = broker.execute(&initial, None)?;
    assert_eq!(result, Ok(Executed::EffectCompleted));
    let permit = permit.ok_or(Error::Recovery)?;

    let publish = intent(Operation::Publish, first.revision, 2, &deny);
    let second = registry.publish(
        &publish,
        &deny,
        &authority.authorize(&publish, "publish-two"),
    )?;
    let (refused, unexpected_permit) = broker.execute(&request("demo-deny"), None)?;
    assert_eq!(
        refused.as_ref().err().map(|e| e.code()),
        Some("M6_SYNTHETIC_FORBID")
    );
    assert!(unexpected_permit.is_none());

    let rollback = intent(Operation::Rollback, second.revision, 1, &allow);
    registry.change(
        &rollback,
        None,
        &authority.authorize(&rollback, "rollback-one"),
    )?;
    let (ledger, effects) = broker.into_stores();
    drop(registry);
    drop(signer);

    let registry = Registry::open(&path, scope(), authority)?;
    let broker = LabBroker::new(&registry, ledger, effects);
    assert_eq!(
        broker.present(&permit, initial.request()),
        Err(Error::StalePermit)
    );
    assert_eq!(
        broker.execute(&request("demo-after-restart"), None)?.0,
        Ok(Executed::EffectCompleted)
    );
    for entry in registry.audit()? {
        println!(
            "audit {}: {} publication {}, activation {}",
            entry.sequence,
            entry.operation.as_str(),
            entry.publication_version,
            entry.revision
        );
    }
    println!("Restart verified the rolled-back pair and refused the stale permit.");
    Ok(())
}
