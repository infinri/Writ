//! The controlled effect adapter interface (M3-04; D-012, D-025 B, D-033 item 2;
//! m1/fixtures/broker/BROKER.md execute step 7 and `present_permit`,
//! LEDGER-SQLITE.md T-adapter and storage rule 6).
//!
//! An effect adapter owns the system the effect happens in and its own store of
//! consumed permit ids, separate from the broker ledger. The broker reaches it
//! only through [`EffectAdapter::apply`]; tests observe the effect store
//! directly, without going through the broker. The local test adapter is
//! `writ_ledger_sqlite::SqliteEffectStore` (a separate SQLite file whose
//! effects are records, not external side effects).
//!
//! **The adapter transaction** (one logical transaction named `adapter`):
//!
//! 1. Begin. If it cannot begin, the answer is `LEDGER_UNAVAILABLE` and no
//!    permit check result is disclosed (D-025 B).
//! 2. Inside it, the broker's verification runs (the `verify` callback):
//!    `PERMIT_REQUEST_MISMATCH`, `PERMIT_INVALID`, `TIME_UNAVAILABLE`,
//!    `PERMIT_EXPIRED`, `PERMIT_STALE` (D-033), in that order
//!    ([`crate::permit::Permit::verify`]); any refusal rolls back.
//! 3. The verified permit id is already recorded as consumed: `PERMIT_REPLAYED`
//!    (rolls back).
//! 4. Otherwise the permit id is recorded as consumed and the effect is
//!    applied (or recorded as failed) in the same transaction, then committed.
//!    A failing commit is `LEDGER_UNAVAILABLE` and leaves neither.
//!
//! An adapter that keeps this contract therefore records at most one effect per
//! permit id in its own durable store, and never an effect without a consumed
//! permit; what that means for a real external system depends on that adapter's
//! own effect, replay and recovery contract (D-035 item 2). It does not make an
//! external system at-most-once or exactly-once: after a failure the broker
//! cannot know whether the commit happened, so the receipt stays `dispatched`
//! and only reconciliation may proceed. The broker's own guarantee is at most
//! one dispatch per (tenant, idempotency key) (D-039).
//!
//! **Inspection** ([`EffectAdapter::inspect`], D-035 item 3): a client-held
//! permit is verified in a read-only adapter transaction (same precedence:
//! storage first, then the permit checks) and its consumption looked up; it
//! never applies an effect. **Evidence** ([`EffectAdapter::evidence`]): the
//! adapter's durable record of what it did with one permit id, which
//! reconciliation may use as authoritative evidence for the exact bound
//! effect. Absence of a record is not evidence that nothing happened outside
//! the adapter's own store.
//!
//! **Ownership** (D-035 item 2): an adapter's store must not be shared by two
//! brokers or two ledgers. The local SQLite test adapter takes its own
//! identity-safe ownership lock and is bound to one ledger; every future
//! production adapter needs its own explicit concurrency, replay,
//! authorization and outcome-recovery contract (OS file locks are not
//! required for remote adapters).
//!
//! **Non-transactional adapters (amendment, M5a-09; M5-INTERFACES J-7.5).**
//! An adapter whose effect cannot be inside its store's transaction (the
//! `fs.write` adapter, `writ-fs-effects`) keeps steps 1 to 3 in its *intent*
//! transaction and replaces step 4 by: record the consumed permit and a
//! durable intent, commit, then perform the effect, then record its outcome
//! in another transaction. Its results:
//!
//! - `Ok(Ok(Applied))` only after the outcome `applied` is committed.
//! - `Ok(Ok(Failed))` only after a committed `failed` outcome with a recorded
//!   reason ([`EffectEvidence::reason`]); for `fs.write` the reasons
//!   `TARGET_UNRESOLVED`, `TARGET_TAINTED`, `TARGET_PRECONDITION_FAILED`,
//!   `STAGE_FAILED` and `drift` are **consumed `Failed` results, not
//!   refusals**: the permit was valid and is consumed, the receipt becomes
//!   `observed_failure`.
//! - `Err(TxFailure { stage: TxStage::Effect, outcome_unknown: true, .. })`
//!   for every other failure after the intent committed: the receipt stays
//!   `dispatched` and only reconciliation may proceed. Commit failures of the
//!   adapter's own transactions keep [`crate::store::TxStage::Commit`].
//! - A store that its adapter disabled answers
//!   `Err(TxFailure { stage: Begin, code: ROOT_UNRECOVERABLE, .. })` before
//!   any permit check; nothing is consumed.
//!
//! `evidence` and `inspect` of such an adapter return a record only for an
//! operation whose outcome is `applied` or `failed`; an unresolved or
//! `unknown` operation yields none, so the receipt stays uncertain
//! (`RECONCILIATION_REQUIRED`). Content equality is never evidence (D-045
//! M5-D8).
//!
//! **Routing by action kind** ([`EffectRoute`], M5a-09, J-7.7): the broker
//! sends [`crate::runner::RUNNER_EFFECT_KIND`] to the runner, [`FS_WRITE_KIND`]
//! to the `fs.write` adapter of the root named by the request's resource, and
//! every other kind to the controlled effect adapter. An `fs.write` request
//! that does not parse ([`FsWriteRequest::parse`]) or names a root without an
//! adapter is refused `REQUEST_SCOPE_MISMATCH` before anything is reserved.

use crate::permit::VerifiedPermit;
use crate::refusal::Refusal;
use crate::store::TxFailure;
use writ_kernel::json::Value;

/// The action kind of the `fs.write` adapter (J-7.1).
pub const FS_WRITE_KIND: &str = "fs.write";

/// The action kind of `request` (`action.kind`), if it is a string.
pub fn action_kind(request: &Value) -> Option<&str> {
    request
        .get("action")
        .and_then(|a| a.get("kind"))
        .and_then(Value::as_str)
}

/// Where the broker sends a request's effect.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EffectRoute {
    /// The controlled effect adapter.
    Controlled,
    /// The runner adapter ([`crate::runner::RUNNER_EFFECT_KIND`]).
    Runner,
    /// The `fs.write` adapter of this root id.
    FsRoot(String),
    /// An `fs.write` request that does not parse ([`FsWriteRequest::parse`]).
    Malformed,
}

impl EffectRoute {
    /// The route of `request` by its action kind.
    pub fn of(request: &Value) -> EffectRoute {
        match action_kind(request) {
            Some(crate::runner::RUNNER_EFFECT_KIND) => EffectRoute::Runner,
            Some(FS_WRITE_KIND) => match FsWriteRequest::parse(request) {
                Some(w) => EffectRoute::FsRoot(w.root_id),
                None => EffectRoute::Malformed,
            },
            _ => EffectRoute::Controlled,
        }
    }
}

/// `fs.write` mode (J-7.1).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FsWriteMode {
    /// The target must not exist.
    CreateOnly,
    /// The target must be a regular single-link file with content
    /// `expected_prior_digest`; it is replaced by `RENAME_EXCHANGE`.
    ReplaceIf,
}

impl FsWriteMode {
    /// `create_only` or `replace_if`.
    pub fn as_str(self) -> &'static str {
        match self {
            FsWriteMode::CreateOnly => "create_only",
            FsWriteMode::ReplaceIf => "replace_if",
        }
    }

    /// The mode named `text`.
    pub fn parse(text: &str) -> Option<FsWriteMode> {
        match text {
            "create_only" => Some(FsWriteMode::CreateOnly),
            "replace_if" => Some(FsWriteMode::ReplaceIf),
            _ => None,
        }
    }
}

/// A parsed `fs.write` request (J-7.1): resource
/// `file:<tenant>/<project>/<root id>/<relative path>` (tenant and project
/// equal to the request's), attributes `snapshot_id`, `manifest_digest`,
/// `source_path`, `content_digest`, `mode` and, for `replace_if` only,
/// `expected_prior_digest`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FsWriteRequest {
    /// Tenant.
    pub tenant_id: String,
    /// Project.
    pub project_id: String,
    /// Root id (`[a-z0-9][a-z0-9-]{0,62}`).
    pub root_id: String,
    /// Canonical relative target path (the target key; [`canonical_target`]).
    pub target: String,
    /// Snapshot id.
    pub snapshot_id: String,
    /// Snapshot manifest digest.
    pub manifest_digest: String,
    /// Path of the source file in the snapshot.
    pub source_path: String,
    /// Digest of the content to write.
    pub content_digest: String,
    /// Mode.
    pub mode: FsWriteMode,
    /// For `replace_if`: the digest the replaced file must have.
    pub expected_prior_digest: Option<String>,
}

/// `true` for a root id: `[a-z0-9][a-z0-9-]{0,62}`.
pub fn valid_root_id(id: &str) -> bool {
    let b = id.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && b[0].is_ascii_lowercase() | b[0].is_ascii_digit()
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// `true` when `path` is a canonical relative target path (J-7.1, F3, F11):
/// components separated by single `/`, no leading or trailing `/`, no empty,
/// `.` or `..` component, no NUL or control character; at most `max_bytes`
/// long and `max_depth` components. No normalization is applied.
pub fn canonical_target(path: &str, max_bytes: usize, max_depth: usize) -> bool {
    !path.is_empty()
        && path.len() <= max_bytes
        && !path.chars().any(char::is_control)
        && path.split('/').count() <= max_depth
        && path
            .split('/')
            .all(|c| !c.is_empty() && c != "." && c != ".." && c.len() <= 255)
}

/// Syntactic bounds the broker applies to `fs.write` targets; each adapter
/// applies its configured (equal or smaller) bounds.
pub const FS_TARGET_MAX_BYTES: usize = 4096;
/// Maximum depth of an `fs.write` target.
pub const FS_TARGET_MAX_DEPTH: usize = 64;

fn digest_text(v: &str) -> bool {
    v.strip_prefix("sha256:").is_some_and(|h| {
        h.len() == 64
            && h.bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    })
}

impl FsWriteRequest {
    /// Parse `request` (any kind check is the caller's); `None` when any
    /// member is missing or invalid.
    pub fn parse(request: &Value) -> Option<FsWriteRequest> {
        let text = |v: Option<&Value>| v.and_then(Value::as_str).map(str::to_string);
        let tenant_id = text(request.get("tenant_id"))?;
        let project_id = text(request.get("project_id"))?;
        let action = request.get("action")?;
        let resource = text(
            action
                .get("resource")
                .and_then(|r| r.get("canonical_resource_id")),
        )?;
        let rest = resource.strip_prefix("file:")?;
        let rest = rest.strip_prefix(tenant_id.as_str())?.strip_prefix('/')?;
        let rest = rest.strip_prefix(project_id.as_str())?.strip_prefix('/')?;
        let (root_id, target) = rest.split_once('/')?;
        if !valid_root_id(root_id)
            || !canonical_target(target, FS_TARGET_MAX_BYTES, FS_TARGET_MAX_DEPTH)
        {
            return None;
        }
        let attrs = action.get("attributes")?;
        let attr = |k: &str| text(attrs.get(k));
        let mode = FsWriteMode::parse(&attr("mode")?)?;
        let expected_prior_digest = attr("expected_prior_digest");
        match (mode, &expected_prior_digest) {
            (FsWriteMode::CreateOnly, None) => {}
            (FsWriteMode::ReplaceIf, Some(d)) if digest_text(d) => {}
            _ => return None,
        }
        let content_digest = attr("content_digest")?;
        let source_path = attr("source_path")?;
        if !digest_text(&content_digest) || !crate::runner::valid_snapshot_path(&source_path) {
            return None;
        }
        Some(FsWriteRequest {
            root_id: root_id.to_string(),
            target: target.to_string(),
            snapshot_id: attr("snapshot_id")?,
            manifest_digest: attr("manifest_digest")?,
            source_path,
            content_digest,
            mode,
            expected_prior_digest,
            tenant_id,
            project_id,
        })
    }
}

/// What the adapter did with a fully verified, newly consumed permit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EffectResult {
    /// The effect was applied (`{"result": "effect_applied"}` for
    /// `present_permit`; the receipt becomes `observed_success`).
    Applied,
    /// The adapter consumed the permit but the effect failed; nothing was
    /// applied (the receipt becomes `observed_failure`).
    Failed,
}

/// A permit that passed the broker's verification for one presented request,
/// with the request's tenant and idempotency key. Built only by the broker
/// inside the adapter transaction; an adapter cannot fabricate one:
///
/// ```compile_fail,E0451
/// let _ = writ_broker::effects::VerifiedEffect {
///     permit_id: String::new(),
///     request_fingerprint: String::new(),
///     tenant_id: String::new(),
///     idempotency_key: String::new(),
///     request: writ_kernel::json::Value::Null,
/// };
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedEffect {
    permit_id: String,
    request_fingerprint: String,
    tenant_id: String,
    idempotency_key: String,
    request: Value,
}

impl VerifiedEffect {
    /// The effect of `permit` for the presented `request` (whose fingerprint
    /// the permit verification already matched). A request without a string
    /// `tenant_id` and `idempotency_key` cannot be attributed: `PERMIT_INVALID`.
    pub(crate) fn new(permit: VerifiedPermit, request: &Value) -> Result<VerifiedEffect, Refusal> {
        let text = |k: &str| {
            request
                .get(k)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or(Refusal::PermitInvalid)
        };
        Ok(VerifiedEffect {
            tenant_id: text("tenant_id")?,
            idempotency_key: text("idempotency_key")?,
            permit_id: permit.permit_id,
            request_fingerprint: permit.request_fingerprint,
            request: request.clone(),
        })
    }

    /// The permit id to record as consumed.
    pub fn permit_id(&self) -> &str {
        &self.permit_id
    }

    /// D-012 fingerprint of the request the effect is for.
    pub fn request_fingerprint(&self) -> &str {
        &self.request_fingerprint
    }

    /// Tenant of the request.
    pub fn tenant_id(&self) -> &str {
        &self.tenant_id
    }

    /// Idempotency key of the request.
    pub fn idempotency_key(&self) -> &str {
        &self.idempotency_key
    }

    /// The presented request (its fingerprint matched the permit), from which
    /// an adapter reads the effect's attributes.
    pub fn request(&self) -> &Value {
        &self.request
    }
}

/// The broker's verification, run by the adapter inside its transaction.
pub type Verify<'v> = dyn FnMut() -> Result<VerifiedEffect, Refusal> + 'v;

/// The adapter's durable record of one consumed permit: the effect it was
/// bound to and what happened (applied, or consumed with a failed effect).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EffectEvidence {
    /// The consumed permit id.
    pub permit_id: String,
    /// Tenant of the request the permit was consumed for.
    pub tenant_id: String,
    /// Idempotency key of that request.
    pub idempotency_key: String,
    /// D-012 fingerprint of that request.
    pub request_fingerprint: String,
    /// What the adapter did.
    pub result: EffectResult,
    /// The recorded reason of a consumed `Failed` result of a
    /// non-transactional adapter (for `fs.write`: `TARGET_UNRESOLVED`,
    /// `TARGET_TAINTED`, `TARGET_PRECONDITION_FAILED`, `STAGE_FAILED`,
    /// `drift`, or the recovery row); `None` for adapters that record none.
    pub reason: Option<String>,
}

impl EffectEvidence {
    /// `true` when the record is for exactly this receipt: same tenant,
    /// idempotency key and request fingerprint.
    pub fn binds(&self, tenant_id: &str, idempotency_key: &str, fingerprint: &str) -> bool {
        self.tenant_id == tenant_id
            && self.idempotency_key == idempotency_key
            && self.request_fingerprint == fingerprint
    }
}

/// A controlled effect adapter with its own store (module documentation).
pub trait EffectAdapter: Sync {
    /// Run the adapter transaction: begin; `verify`; replay check; record the
    /// consumed permit id and apply the effect atomically; commit. The outer
    /// `Err` is a failed transaction (with its stage, and whether an issued
    /// commit has an unknown outcome); the inner `Err` is a permit refusal
    /// (rolled back, nothing recorded).
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure>;

    /// Inspect a presented permit without applying anything: begin a
    /// read-only transaction; `verify`; return the evidence recorded for the
    /// verified permit id (`None`: not consumed); roll back. A storage
    /// failure is the outer `Err` and takes precedence over every permit
    /// check (D-025 B, D-035 item 4).
    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure>;

    /// The evidence recorded for `permit_id`, if any (read-only transaction).
    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use writ_kernel::json;

    const D: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

    fn request(resource: &str, attrs: &str) -> Value {
        let text = format!(
            r#"{{"tenant_id":"t","project_id":"p","action":{{"kind":"fs.write",
            "resource":{{"uri":"file:///x","canonical_resource_id":"{resource}"}},"attributes":{attrs}}}}}"#
        );
        json::parse(text.as_bytes()).unwrap()
    }

    fn attrs(mode: &str, prior: Option<&str>) -> String {
        let prior = prior.map_or(String::new(), |p| {
            format!(r#","expected_prior_digest":"{p}""#)
        });
        format!(
            r#"{{"snapshot_id":"snap-1","manifest_digest":"{D}","source_path":"src/a.txt",
            "content_digest":"{D}","mode":"{mode}"{prior}}}"#
        )
    }

    #[test]
    fn fs_write_requests_parse_only_in_their_canonical_form() {
        let ok = FsWriteRequest::parse(&request(
            "file:t/p/r1/dir/a.txt",
            &attrs("create_only", None),
        ))
        .unwrap();
        assert_eq!(
            (ok.root_id.as_str(), ok.target.as_str()),
            ("r1", "dir/a.txt")
        );
        assert_eq!(ok.mode, FsWriteMode::CreateOnly);
        let rep = FsWriteRequest::parse(&request("file:t/p/r1/a", &attrs("replace_if", Some(D))))
            .unwrap();
        assert_eq!(rep.expected_prior_digest.as_deref(), Some(D));
        for bad in [
            "file:t/p/r1/",
            "file:t/p/r1//a",
            "file:t/p/r1/a/",
            "file:t/p/r1/./a",
            "file:t/p/r1/../a",
            "file:t/p/r1/a\\u0001b",
            "file:t/p/R1/a",
            "file:t/q/r1/a",
            "file:u/p/r1/a",
            "workspace:t/p/r1/a",
            "file:t/p/r1",
        ] {
            assert!(
                FsWriteRequest::parse(&request(bad, &attrs("create_only", None))).is_none(),
                "{bad}"
            );
        }
        let r = "file:t/p/r1/a";
        assert!(FsWriteRequest::parse(&request(r, &attrs("create_only", Some(D)))).is_none());
        assert!(FsWriteRequest::parse(&request(r, &attrs("replace_if", None))).is_none());
        assert!(FsWriteRequest::parse(&request(r, &attrs("replace", None))).is_none());
        assert!(
            FsWriteRequest::parse(&request(r, &attrs("replace_if", Some("sha256:AB")))).is_none()
        );
        assert_eq!(
            EffectRoute::of(&request(r, &attrs("create_only", None))),
            EffectRoute::FsRoot("r1".into())
        );
        assert_eq!(
            EffectRoute::of(&request("file:t/p/r1/", &attrs("create_only", None))),
            EffectRoute::Malformed
        );
        let other = json::parse(br#"{"action":{"kind":"file.write"}}"#).unwrap();
        assert_eq!(EffectRoute::of(&other), EffectRoute::Controlled);
        let deep = vec!["a"; 70].join("/");
        assert!(!canonical_target(&deep, 4096, 64));
        assert!(canonical_target(&deep, 4096, 70));
    }
}
