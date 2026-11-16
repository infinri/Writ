//! The normal path of `fs.write` (J-7.5): T-intent, stage, T-staged, recheck, rename,
//! durability, verify, T-outcome, cleanup; results and crash boundaries B0..B12.

use std::os::fd::OwnedFd;
use std::sync::PoisonError;

use writ_broker::effects::{canonical_target, EffectResult, FsWriteMode, FsWriteRequest, Verify};
use writ_broker::refusal::Refusal;
use writ_broker::runner::snapshot_digest;
use writ_broker::store::TxFailure;
use writ_hostsys::fsfx;
use writ_ledger_sqlite::{FsEnd, OpRow, OpState, OutcomeRow, TargetRow};

use crate::ident::{self, Identity, Prior};
use crate::{effect_failure, Boundary, FsWriteAdapter, Parent, Slot, Step};

/// Consumed `Failed` reasons (J-7.5; not refusals).
pub const TARGET_UNRESOLVED: &str = "TARGET_UNRESOLVED";
/// See [`TARGET_UNRESOLVED`].
pub const TARGET_TAINTED: &str = "TARGET_TAINTED";
/// See [`TARGET_UNRESOLVED`].
pub const TARGET_PRECONDITION_FAILED: &str = "TARGET_PRECONDITION_FAILED";
/// See [`TARGET_UNRESOLVED`].
pub const STAGE_FAILED: &str = "STAGE_FAILED";
/// `EEXIST` from `RENAME_NOREPLACE` (F8).
pub const DRIFT: &str = "drift";

/// What T-intent decided.
enum Intent {
    Refused(Refusal),
    /// A consumed `Failed` result was committed with this reason.
    Failed,
    /// The target could not be observed: rolled back, nothing consumed.
    Unobservable(String),
    Proceed(Box<Op>),
}

/// An op in flight.
pub(crate) struct Op {
    pub(crate) row: OpRow,
    pub(crate) req: FsWriteRequest,
    pub(crate) prior: Prior,
}

/// Removes the op from the live set when the applying thread leaves `apply` (M6: acquired
/// and released by that thread only).
struct LiveEntry<'a, 'b> {
    adapter: &'a FsWriteAdapter<'b>,
    op_id: String,
}

impl Drop for LiveEntry<'_, '_> {
    fn drop(&mut self) {
        self.adapter
            .live
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(&self.op_id);
    }
}

fn outcome(
    op_id: &str,
    evidence: &str,
    desired: &str,
    result: &str,
    reason: &str,
    boot: &str,
) -> OutcomeRow {
    OutcomeRow {
        op_id: op_id.to_string(),
        operation_evidence: evidence.to_string(),
        desired_state: desired.to_string(),
        result: result.to_string(),
        reason: reason.to_string(),
        target_identity_after: None,
        displaced_identity: None,
        boot_id: boot.to_string(),
    }
}

/// `desired_state` of an observed target slot for an op whose staged identity is `staged`
/// (L1).
pub(crate) fn desired_state(
    t: &Slot,
    staged: Option<&Identity>,
    content_digest: &str,
) -> &'static str {
    match t {
        Slot::Absent => "absent",
        s => match staged {
            Some(id) if s.same(id) && s.digest() == Some(content_digest) => "verified",
            _ => "differs",
        },
    }
}

impl FsWriteAdapter<'_> {
    pub(crate) fn apply_effect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        // The adapter-wide lock, held by this thread until it returns (F1, L-a, M6).
        let guard = self.lock();
        if self.is_disabled() {
            return Err(Self::unrecoverable());
        }
        let intent = self.jwrite("fs_intent", |tx| {
            let effect = match verify() {
                Ok(e) => e,
                Err(r) => return Ok(FsEnd::Rollback(Intent::Refused(r))),
            };
            if tx.op_by_permit(effect.permit_id())?.is_some() {
                return Ok(FsEnd::Rollback(Intent::Refused(Refusal::PermitReplayed)));
            }
            let req = match FsWriteRequest::parse(effect.request()) {
                Some(r) if r.root_id == self.cfg.root_id => r,
                _ => {
                    return Ok(FsEnd::Rollback(Intent::Refused(
                        Refusal::RequestScopeMismatch,
                    )))
                }
            };
            let op_id = format!("fsop-{}", effect.permit_id());
            let mut row = OpRow {
                staging_name: self.staging_name(&op_id),
                op_id,
                permit_id: effect.permit_id().to_string(),
                tenant_id: effect.tenant_id().to_string(),
                idempotency_key: effect.idempotency_key().to_string(),
                request_fingerprint: effect.request_fingerprint().to_string(),
                root_id: req.root_id.clone(),
                target: req.target.clone(),
                mode: req.mode.as_str().to_string(),
                expected_prior: "absent".into(),
                content_digest: req.content_digest.clone(),
                snapshot_id: req.snapshot_id.clone(),
                state: OpState::Resolved,
                staged_identity: None,
                owner_epoch: 0,
            };
            let failed = |row: &OpRow,
                          reason: &str,
                          desired: &str|
             -> Result<FsEnd<Intent>, writ_ledger_sqlite::FsTxError> {
                tx.insert_op(row)?;
                tx.insert_outcome(&outcome(
                    &row.op_id,
                    "not_completed",
                    desired,
                    "failed",
                    reason,
                    &self.boot_id,
                ))?;
                Ok(FsEnd::Commit(Intent::Failed))
            };
            // Target checks through tree_fd (precondition results).
            if !canonical_target(&req.target, self.cfg.max_path_bytes, self.cfg.max_depth) {
                return failed(&row, TARGET_PRECONDITION_FAILED, "unknown");
            }
            let t = match self.target_slot(&req.target) {
                Ok((Parent::Found(..), t)) => t,
                Ok((_, _)) => return failed(&row, TARGET_PRECONDITION_FAILED, "unknown"),
                Err(e) => return Ok(FsEnd::Rollback(Intent::Unobservable(e.code()))),
            };
            let desired = desired_state(&t, None, &req.content_digest);
            let prior = match (req.mode, &t) {
                (FsWriteMode::CreateOnly, Slot::Absent) => Prior::Absent,
                (
                    FsWriteMode::ReplaceIf,
                    Slot::Present {
                        o,
                        digest: Some(d),
                        acl: false,
                    },
                ) if o.is_regular()
                    && o.nlink == 1
                    && Some(d.as_str()) == req.expected_prior_digest.as_deref() =>
                {
                    Prior::File(Identity::of(&self.cfg.fs_uuid, o, d))
                }
                _ => return failed(&row, TARGET_PRECONDITION_FAILED, desired),
            };
            row.expected_prior = prior.text();
            let target_row = tx.target(&req.target)?;
            if let Some(tr) = &target_row {
                if tr.blocked_by.is_some() {
                    return failed(&row, TARGET_UNRESOLVED, desired);
                }
                if tr.tainted {
                    return failed(&row, TARGET_TAINTED, desired);
                }
                // R11: the target differs from the last identity the adapter established.
                if let Some(last) = &tr.last_identity {
                    let same = match (Prior::parse(last), &prior) {
                        (Some(Prior::Absent), Prior::Absent) => true,
                        (Some(Prior::File(a)), Prior::File(b)) => a == *b,
                        _ => false,
                    };
                    if !same {
                        tx.put_target(&TargetRow {
                            tainted: true,
                            taint_reason: Some("R11".into()),
                            ..tr.clone()
                        })?;
                        return failed(&row, TARGET_TAINTED, desired);
                    }
                }
            }
            row.state = OpState::Intent;
            tx.insert_op(&row)?;
            tx.put_target(&TargetRow {
                target: req.target.clone(),
                blocked_by: Some(row.op_id.clone()),
                tainted: false,
                taint_reason: None,
                last_identity: target_row.and_then(|r| r.last_identity),
            })?;
            self.boundary(Boundary::B0);
            Ok(FsEnd::Commit(Intent::Proceed(Box::new(Op {
                row,
                req,
                prior,
            }))))
        });
        let op = match intent {
            // B1 with an unknown commit outcome: stop (F6); the journal is reopened before
            // anything reads it.
            Err(f) => return Err(f),
            Ok(Intent::Refused(r)) => return Ok(Err(r)),
            Ok(Intent::Failed) => return Ok(Ok(EffectResult::Failed)),
            Ok(Intent::Unobservable(code)) => {
                return Err(TxFailure::new(
                    writ_broker::store::TxStage::Body,
                    writ_broker::store::StoreError::Unavailable {
                        category: "transaction fs_intent".into(),
                        code: format!("observe {code}"),
                    },
                ))
            }
            Ok(Intent::Proceed(op)) => op,
        };
        self.live
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(op.row.op_id.clone());
        let _live = LiveEntry {
            adapter: self,
            op_id: op.row.op_id.clone(),
        };
        self.boundary(Boundary::B1);
        let out = self.after_intent(&guard, &op);
        self.boundary(Boundary::B12);
        out
    }

    fn after_intent(
        &self,
        guard: &std::sync::MutexGuard<'_, ()>,
        op: &Op,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let name = op.row.staging_name.as_str();
        // Stage: source bytes from the snapshot store, digests re-verified.
        let Some(bytes) = self.source(&op.req) else {
            return self.resolve_no_file(op);
        };
        let fd = match self
            .fault(Step::Create)
            .and_then(|()| fsfx::create_exclusive(&self.staging, name))
        {
            Ok(fd) => fd,
            Err(e) if e.is_exists() => {
                // L2: drift; the existing file is not removed.
                self.taint(&op.row.target, "staging_exists")?;
                return Err(effect_failure("staging exists"));
            }
            Err(_) => return self.resolve_no_file(op),
        };
        let half = bytes.len() / 2;
        let written = self.fault(Step::Write).and_then(|()| {
            fsfx::write_all(&fd, &bytes[..half])?;
            self.boundary(Boundary::B2);
            fsfx::write_all(&fd, &bytes[half..])
        });
        if written.is_err() {
            return self.abandon(op, &fd);
        }
        self.boundary(Boundary::B3);
        self.sync(&fd, Step::FsyncFile, "staged")?;
        let reread = fsfx::open_read_beneath(&self.staging, name)
            .and_then(|r| fsfx::read_to_end(r, self.cfg.max_file_bytes));
        let staged_ok = matches!(&reread, Ok(b) if ident::digest(b) == op.req.content_digest);
        let Ok(o) = self
            .fault(Step::StatxStaged)
            .and_then(|()| fsfx::observe(&fd))
        else {
            // Without a birth time the identity cannot be recorded: nothing is removed.
            return Err(effect_failure("statx staged"));
        };
        let acl = fsfx::acls(&fd).map(|a| a.0).unwrap_or(true);
        if !staged_ok || acl || o.nlink != 1 || o.mnt_id != self.mnt_id || o.dev != self.dev() {
            return self.abandon(op, &fd);
        }
        self.sync(&self.staging, Step::FsyncStaging, "staging")?;
        let staged = Identity::of(&self.cfg.fs_uuid, &o, &op.req.content_digest);
        self.boundary(Boundary::B4);
        // T-staged.
        self.jwrite("fs_staged", |tx| {
            tx.set_op_state(
                &op.row.op_id,
                OpState::Intent,
                OpState::Staged,
                Some(&staged.text()),
            )?;
            #[cfg(test)]
            if self.is_mutant(crate::Mutant::ReleaseBlockEarly) {
                if let Some(row) = tx.target(&op.row.target)? {
                    tx.put_target(&TargetRow {
                        blocked_by: None,
                        ..row
                    })?;
                }
            }
            Ok(FsEnd::Commit(()))
        })?;
        self.boundary(Boundary::B5);
        // Recheck (F3).
        self.boundary(Boundary::B6);
        let s = self.slot(&self.staging, name);
        let t = self.target_slot(&op.req.target);
        let (parent, leaf) = match (&s, t) {
            (Ok(s), Ok((Parent::Found(dir, leaf), t)))
                if s.same(&staged) && s.nlink() == 1 && t.is_prior(&op.prior) =>
            {
                (dir, leaf)
            }
            _ => return self.reobserve(guard, op),
        };
        self.boundary(Boundary::B7);
        // Rename (F4).
        let renamed = self.fault(Step::Rename).and_then(|()| match op.req.mode {
            FsWriteMode::CreateOnly => fsfx::rename_noreplace(&self.staging, name, &parent, &leaf),
            FsWriteMode::ReplaceIf => fsfx::rename_exchange(&self.staging, name, &parent, &leaf),
        });
        match renamed {
            Ok(()) => {}
            Err(e) if e.is_exists() && op.req.mode == FsWriteMode::CreateOnly => {
                return self.drift(op);
            }
            // Any other rename error: the table decides, never the errno alone (F8).
            Err(_) => return self.reobserve(guard, op),
        }
        self.boundary(Boundary::B8);
        // Durability.
        self.sync(&parent, Step::FsyncParent, "parent")?;
        self.sync(&self.staging, Step::FsyncStaging, "staging")?;
        self.boundary(Boundary::B9);
        // Verify through a fresh resolution from tree (a moved parent is caught here).
        let t = self.target_slot(&op.req.target);
        let s = self.slot(&self.staging, name);
        let verified = match (&t, &s, &op.prior) {
            (Ok((Parent::Found(..), t)), Ok(s), prior) => {
                let target_ok = t.same(&staged)
                    && t.nlink() == 1
                    && t.digest() == Some(op.req.content_digest.as_str());
                let staging_ok = match prior {
                    Prior::Absent => *s == Slot::Absent,
                    Prior::File(p) => {
                        s.same(p)
                            && s.nlink() == 1
                            && s.digest() == op.req.expected_prior_digest.as_deref()
                    }
                };
                target_ok && staging_ok
            }
            _ => false,
        };
        if !verified {
            return self.reobserve(guard, op);
        }
        self.boundary(Boundary::B10);
        // T-outcome.
        self.jwrite("fs_outcome", |tx| {
            let mut out = outcome(
                &op.row.op_id,
                "completed",
                "verified",
                "applied",
                "applied",
                &self.boot_id,
            );
            out.target_identity_after = Some(staged.text());
            if let Prior::File(p) = &op.prior {
                out.displaced_identity = Some(p.text());
            }
            tx.resolve(&out)?;
            tx.put_target(&TargetRow {
                target: op.row.target.clone(),
                blocked_by: None,
                tainted: false,
                taint_reason: None,
                last_identity: Some(staged.text()),
            })?;
            Ok(FsEnd::Commit(()))
        })?;
        self.boundary(Boundary::B11);
        // Cleanup (F5, N2): only the recorded displaced identity is removed.
        if let Prior::File(p) = &op.prior {
            if self.fault(Step::Cleanup).is_ok()
                && self.slot(&self.staging, name).is_ok_and(|s| s.same(p))
                && fsfx::unlink_at(&self.staging, name).is_ok()
            {
                let _ = self.sync(&self.staging, Step::FsyncStaging, "staging");
            }
        }
        Ok(Ok(EffectResult::Applied))
    }

    /// The source bytes of `req` from the snapshot store: the store must be immutable, the
    /// manifest digest and the file's digest must equal the request's.
    fn source(&self, req: &FsWriteRequest) -> Option<Vec<u8>> {
        if !self.snapshots.immutable() {
            return None;
        }
        let files = self
            .snapshots
            .content(&req.tenant_id, &req.project_id, &req.snapshot_id)
            .ok()??;
        if snapshot_digest(&files)? != req.manifest_digest {
            return None;
        }
        let file = files.into_iter().find(|f| f.path == req.source_path)?;
        (ident::digest(&file.bytes) == req.content_digest
            && file.bytes.len() as u64 <= self.cfg.max_file_bytes)
            .then_some(file.bytes)
    }

    /// A staging failure before any file was created: resolve `failed` `STAGE_FAILED`
    /// inline (sound like R1a: no rename precedes a durable T-staged, and no file exists).
    fn resolve_no_file(&self, op: &Op) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        self.resolve_failed(op, STAGE_FAILED)?;
        Ok(Ok(EffectResult::Failed))
    }

    fn resolve_failed(&self, op: &Op, reason: &str) -> Result<(), TxFailure> {
        self.jwrite("fs_resolve", |tx| {
            tx.resolve(&outcome(
                &op.row.op_id,
                "not_completed",
                if op.prior == Prior::Absent {
                    "absent"
                } else {
                    "differs"
                },
                "failed",
                reason,
                &self.boot_id,
            ))?;
            tx.put_target(&TargetRow {
                target: op.row.target.clone(),
                blocked_by: None,
                tainted: false,
                taint_reason: None,
                last_identity: Some(op.prior.text()),
            })?;
            Ok(FsEnd::Commit(()))
        })
    }

    /// L-b: a staging failure after a successful create. (1) commit `abandoned` with the
    /// file's identity, (2) unlink the own file and `fsync(staging)`, (3) commit `failed`
    /// `STAGE_FAILED`. If (1) cannot commit, nothing is removed.
    fn abandon(&self, op: &Op, fd: &OwnedFd) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let Ok(o) = fsfx::observe(fd) else {
            return Err(effect_failure("statx abandoned"));
        };
        let name = op.row.staging_name.as_str();
        let digest = fsfx::open_read_beneath(&self.staging, name)
            .and_then(|r| fsfx::read_to_end(r, self.cfg.max_file_bytes))
            .map(|b| ident::digest(&b))
            .unwrap_or_else(|_| "unreadable".into());
        let own = Identity::of(&self.cfg.fs_uuid, &o, &digest);
        self.jwrite("fs_abandon", |tx| {
            tx.set_op_state(
                &op.row.op_id,
                OpState::Intent,
                OpState::Abandoned,
                Some(&own.text()),
            )?;
            Ok(FsEnd::Commit(()))
        })
        .map_err(|f| {
            if f.outcome_unknown {
                f
            } else {
                effect_failure("abandon")
            }
        })?;
        self.boundary(Boundary::Abandoned);
        if self.slot(&self.staging, name).is_ok_and(|s| s.same(&own)) {
            let _ = fsfx::unlink_at(&self.staging, name);
        }
        self.sync(&self.staging, Step::FsyncStaging, "staging")?;
        self.boundary(Boundary::AbandonUnlinked);
        self.resolve_failed(op, STAGE_FAILED)?;
        Ok(Ok(EffectResult::Failed))
    }

    /// `EEXIST` from `RENAME_NOREPLACE` (F8): not applied, `failed` reason `drift`, target
    /// tainted; the staged file stays (the target is tainted, so it is never collected).
    fn drift(&self, op: &Op) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        self.jwrite("fs_drift", |tx| {
            tx.resolve(&outcome(
                &op.row.op_id,
                "not_completed",
                "differs",
                "failed",
                DRIFT,
                &self.boot_id,
            ))?;
            let row = tx.target(&op.row.target)?;
            tx.put_target(&TargetRow {
                target: op.row.target.clone(),
                blocked_by: None,
                tainted: true,
                taint_reason: Some(DRIFT.into()),
                last_identity: row.and_then(|r| r.last_identity),
            })?;
            Ok(FsEnd::Commit(()))
        })?;
        Ok(Ok(EffectResult::Failed))
    }

    /// Taint `target` in its own transaction (the op stays unresolved for recovery).
    fn taint(&self, target: &str, reason: &str) -> Result<(), TxFailure> {
        self.jwrite("fs_taint", |tx| {
            let row = tx.target(target)?;
            tx.put_target(&TargetRow {
                target: target.to_string(),
                blocked_by: row.as_ref().and_then(|r| r.blocked_by.clone()),
                tainted: true,
                taint_reason: Some(reason.into()),
                last_identity: row.and_then(|r| r.last_identity),
            })?;
            Ok(FsEnd::Commit(()))
        })
        .map_err(|f| {
            if f.outcome_unknown {
                f
            } else {
                effect_failure("taint")
            }
        })
    }

    /// Re-observation under the guard `apply` holds (F8, L-a): the decision table for this
    /// op; a committed `applied` or `failed` row is the result, anything else is an
    /// effect-stage failure with an unknown outcome.
    fn reobserve(
        &self,
        guard: &std::sync::MutexGuard<'_, ()>,
        op: &Op,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let rows = self.recover_locked(guard, Some(&op.row.op_id))?;
        let row = rows.first().map_or("R9", |(_, r)| *r);
        match crate::recover::result_of(row) {
            Some("applied") => Ok(Ok(EffectResult::Applied)),
            Some("failed") => Ok(Ok(EffectResult::Failed)),
            _ => Err(effect_failure(&format!("reobserved {row}"))),
        }
    }
}
