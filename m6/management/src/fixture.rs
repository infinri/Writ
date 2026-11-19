//! Explicit test-only credentials for the disposable CLI. All values are public fixtures.

use crate::{Authenticator, Grant, Principal, Realm, Role, Scope};

pub fn authority() -> impl Authenticator {
    FixtureAuthority
}

struct FixtureAuthority;

impl Authenticator for FixtureAuthority {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        let (realm, subject, role) = match credential {
            b"fixture-author" => (Realm::Management, "fixture-author", Role::PolicyAuthor),
            b"fixture-viewer" => (Realm::Management, "fixture-viewer", Role::Viewer),
            b"fixture-agent" => (Realm::Agent, "fixture-agent", Role::PolicyAuthor),
            b"fixture-adapter" => (Realm::Adapter, "fixture-adapter", Role::PolicyAuthor),
            _ => return None,
        };
        Some(Principal {
            realm,
            subject: subject.into(),
            grants: vec![Grant {
                scope: Scope {
                    tenant: "m6-lab".into(),
                    project: "offline-proof".into(),
                },
                role,
            }],
        })
    }
}
