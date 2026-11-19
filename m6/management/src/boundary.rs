use writ_policy_publication_lab::{
    Audit, Commit, Error, Intent, Publication, Registry, Scope, TrustAuthority,
};

/// Host-configured access to the registry. The management service receives neither keys nor a signer.
pub trait PublicationBoundary {
    fn scope(&self) -> &Scope;
    fn preview(
        &self,
        expected_revision: i64,
        publication_version: i64,
        candidate: Option<&Publication>,
    ) -> Result<Publication, Error>;
    fn preview_retire(
        &self,
        _expected_revision: i64,
        _publication_version: i64,
        _confirmation: &str,
    ) -> Result<Publication, Error> {
        Err(Error::DependencyUnavailable)
    }
    fn preview_optional_removal(
        &self,
        _expected_revision: i64,
        _replacement_version: i64,
        _candidate: &Publication,
    ) -> Result<writ_kernel::json::Value, Error> {
        Err(Error::Unsupported)
    }
    fn refuse_deactivation(
        &self,
        _expected_revision: i64,
        _publication_version: i64,
        _policy_id: &str,
        _confirmation: &str,
    ) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn change(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        presentation: &[u8],
        reauthorize: &dyn Fn() -> crate::Result<()>,
    ) -> Result<Commit, Error>;
    fn audit(&self) -> Result<Vec<Audit>, Error>;
    fn current_selection(&self) -> Result<Option<Commit>, Error>;
}

impl<T: TrustAuthority> PublicationBoundary for Registry<T> {
    fn scope(&self) -> &Scope {
        Registry::scope(self)
    }

    fn preview(
        &self,
        expected_revision: i64,
        publication_version: i64,
        candidate: Option<&Publication>,
    ) -> Result<Publication, Error> {
        Registry::preview(self, expected_revision, publication_version, candidate)
    }

    fn preview_retire(
        &self,
        expected_revision: i64,
        publication_version: i64,
        confirmation: &str,
    ) -> Result<Publication, Error> {
        Registry::preview_retire(self, expected_revision, publication_version, confirmation)
    }

    fn preview_optional_removal(
        &self,
        expected_revision: i64,
        replacement_version: i64,
        candidate: &Publication,
    ) -> Result<writ_kernel::json::Value, Error> {
        Registry::preview_optional_removal(self, expected_revision, replacement_version, candidate)
    }

    fn refuse_deactivation(
        &self,
        expected_revision: i64,
        publication_version: i64,
        policy_id: &str,
        confirmation: &str,
    ) -> Result<(), Error> {
        Registry::refuse_deactivation(
            self,
            expected_revision,
            publication_version,
            policy_id,
            confirmation,
        )
    }

    fn change(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        presentation: &[u8],
        reauthorize: &dyn Fn() -> crate::Result<()>,
    ) -> Result<Commit, Error> {
        Registry::change_managed(self, intent, candidate, presentation, || {
            reauthorize().map_err(|error| match error {
                crate::Error::Unauthorized | crate::Error::NotFound => Error::Authority,
                _ => Error::Storage,
            })
        })
    }

    fn audit(&self) -> Result<Vec<Audit>, Error> {
        Registry::audit(self)
    }

    fn current_selection(&self) -> Result<Option<Commit>, Error> {
        Registry::current_selection(self)
    }
}
