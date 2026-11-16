//! Recovery and reconciliation (J-7.6) and garbage collection (J-7.5 cleanup rule).
//!
//! Only ops in state `intent`, `abandoned` or `staged` are processed; `resolved` ops,
//! including `unknown` ones, are never revisited (N2). Before observing, recovery makes the
//! observed state durable (`fsync` of the target's parent, the target if present and
//! `staging`; an error marks the root and concludes nothing). Rows are tried top to bottom and
//! the first match decides (N1); content equality alone is never evidence.

use std::sync::{MutexGuard, PoisonError};

use writ_broker::effects::FsWriteMode;
use writ_broker::store::{StoreError, TxFailure, TxStage};
use writ_hostsys::fsfx;
use writ_ledger_sqlite::{FsEnd, OpRow, OpState, OutcomeRow, TargetRow};

use crate::apply::{desired_state, STAGE_FAILED};
use crate::ident::{Identity, Prior};
use crate::{FsWriteAdapter, Parent, Slot, Step};

/// What a row does to the target.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum After {
    /// Unblocked; `last_identity` set to the prior state.
    UnblockPrior,
    /// Unblocked; `last_identity` set to the staged identity.
    UnblockStaged,
    /// Stays blocked by the op until a disposition (R1b).
    Blocked,
    /// Tainted; every file kept.
    Taint,
}

/// Which staging file a row removes after its commit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Remove {
    Nothing,
    Staged,
    Displaced,
}

/// (operation evidence, result, target after, removal) of a row.
fn plan(row: &str) -> Option<(&'static str, &'static str, After, Remove)> {
    Some(match row {
        "R1a" => (
            "not_completed",
            "failed",
            After::UnblockPrior,
            Remove::Nothing,
        ),
        "R1b" => ("not_completed", "failed", After::Blocked, Remove::Nothing),
        "R1c" => (
            "not_completed",
            "failed",
            After::UnblockPrior,
            Remove::Staged,
        ),
        "R3" => (
            "completed",
            "applied",
            After::UnblockStaged,
            Remove::Nothing,
        ),
        "R3x" => (
            "completed",
            "applied",
            After::UnblockStaged,
            Remove::Displaced,
        ),
        "R5" => (
            "not_completed",
            "failed",
            After::UnblockPrior,
            Remove::Staged,
        ),
        "R2" | "R4" | "R6" | "R7" | "R8" | "R3y" | "R-other" => {
            ("unknown", "unknown", After::Taint, Remove::Nothing)
        }
        _ => return None,
    })
}

/// The recorded result of a row (`applied`, `failed`, `unknown`); `None` for R9 (nothing
/// recorded).
pub fn result_of(row: &str) -> Option<&'static str> {
    plan(row).map(|p| p.1)
}

/// What the journal records about one unresolved op.
pub(crate) struct Facts<'a> {
    pub(crate) state: OpState,
    pub(crate) mode: FsWriteMode,
    pub(crate) prior: &'a Prior,
    pub(crate) staged: Option<&'a Identity>,
    pub(crate) content_digest: &'a str,
    pub(crate) expected_prior_digest: Option<&'a str>,
}

/// The J-7.6 decision for one op, from its journal facts and the observations of the
/// target `t` and the staging name `s` (first match wins; R9 is decided by the caller).
pub(crate) fn decide(f: &Facts<'_>, t: &Slot, s: &Slot) -> &'static str {
    let Facts {
        state,
        mode,
        prior,
        staged,
        content_digest,
        expected_prior_digest,
    } = *f;
    let t_prior = t.is_prior(prior);
    match state {
        OpState::Intent => {
            if !t_prior {
                "R2"
            } else if *s == Slot::Absent {
                "R1a"
            } else {
                "R1b"
            }
        }
        OpState::Abandoned => match staged {
            Some(id) if t_prior && (*s == Slot::Absent || s.same(id)) => "R1c",
            _ => "R-other",
        },
        OpState::Staged => {
            let Some(id) = staged else {
                return "R-other";
            };
            let t_staged = t.same(id);
            let s_staged = s.same(id);
            let s_prior_ident = match prior {
                Prior::File(p) => s.same(p),
                Prior::Absent => false,
            };
            let s_other = *s != Slot::Absent
                && !s_staged
                && !(mode == FsWriteMode::ReplaceIf && s_prior_ident);
            let t_digest_ok = t.digest() == Some(content_digest);
            if s_other
                || (t_staged && t.nlink() > 1)
                || (s_staged && s.nlink() > 1)
                || (t_staged && s_staged)
            {
                return "R8";
            }
            if t_staged && !t_digest_ok {
                return "R4";
            }
            if mode == FsWriteMode::ReplaceIf
                && t_staged
                && (*s == Slot::Absent
                    || !s_prior_ident
                    || s.digest() != expected_prior_digest
                    || s.nlink() > 1)
            {
                return "R3y";
            }
            if s_staged && !t_prior && !t_staged {
                return "R6";
            }
            if mode == FsWriteMode::CreateOnly
                && t_staged
                && t.nlink() == 1
                && *s == Slot::Absent
                && t_digest_ok
            {
                return "R3";
            }
            if mode == FsWriteMode::ReplaceIf
                && t_staged
                && t.nlink() == 1
                && s_prior_ident
                && s.nlink() == 1
                && s.digest() == expected_prior_digest
                && t_digest_ok
            {
                return "R3x";
            }
            if s_staged && s.nlink() == 1 && t_prior {
                return "R5";
            }
            if mode == FsWriteMode::CreateOnly && *s == Slot::Absent && !t_staged {
                return "R7";
            }
            "R-other"
        }
        OpState::Resolved => "R-other",
    }
}

fn integrity() -> TxFailure {
    TxFailure::new(
        TxStage::Body,
        StoreError::Unavailable {
            category: "fs_journal recover".into(),
            code: "integrity".into(),
        },
    )
}

impl FsWriteAdapter<'_> {
    /// Recovery under the adapter-wide lock or `apply`'s guard (L-a): every unresolved op
    /// (skipping live applies), or only `only`. Returns the row each op reached.
    pub(crate) fn recover_locked(
        &self,
        _guard: &MutexGuard<'_, ()>,
        only: Option<&str>,
    ) -> Result<Vec<(String, &'static str)>, TxFailure> {
        if self.is_disabled() {
            return Err(Self::unrecoverable());
        }
        let ops = self.jread("fs_recover_scan", |tx| tx.unresolved_ops())?;
        let live = self
            .live
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let mut out = Vec::new();
        for op in ops {
            match only {
                Some(id) if id != op.op_id => continue,
                None if live.contains(&op.op_id) => continue,
                _ => {}
            }
            let row = self.recover_op(&op)?;
            out.push((op.op_id.clone(), row));
            if self.is_disabled() {
                break;
            }
        }
        Ok(out)
    }

    fn recover_op(&self, op: &OpRow) -> Result<&'static str, TxFailure> {
        let prior = Prior::parse(&op.expected_prior).ok_or_else(integrity)?;
        let staged = match &op.staged_identity {
            Some(t) => Some(Identity::parse(t).ok_or_else(integrity)?),
            None => None,
        };
        let mode = FsWriteMode::parse(&op.mode).ok_or_else(integrity)?;
        let expected_prior_digest = match &prior {
            Prior::File(p) => Some(p.digest.clone()),
            Prior::Absent => None,
        };
        // Durability of the observed state, then the observations (R9 on any failure).
        let parent = match self
            .fault(Step::ObserveTarget)
            .map_err(Into::into)
            .and_then(|()| self.parent(&op.target))
        {
            Ok(p) => p,
            Err(_) => return Ok("R9"),
        };
        let t = match &parent {
            Parent::Unsafe => return Ok("R9"),
            Parent::Missing => Slot::Absent,
            Parent::Found(dir, leaf) => {
                if self.sync(dir, Step::FsyncParent, "parent").is_err() {
                    return Ok("R9");
                }
                if let Ok(fd) = fsfx::open_read_beneath(dir, leaf) {
                    if self.sync(&fd, Step::FsyncFile, "target").is_err() {
                        return Ok("R9");
                    }
                }
                match self.slot(dir, leaf) {
                    Ok(t) => t,
                    Err(_) => return Ok("R9"),
                }
            }
        };
        if self
            .sync(&self.staging, Step::FsyncStaging, "staging")
            .is_err()
        {
            return Ok("R9");
        }
        let s = match self.slot(&self.staging, &op.staging_name) {
            Ok(s) => s,
            Err(_) => return Ok("R9"),
        };
        let facts = Facts {
            state: op.state,
            mode,
            prior: &prior,
            staged: staged.as_ref(),
            content_digest: &op.content_digest,
            expected_prior_digest: expected_prior_digest.as_deref(),
        };
        let row = decide(&facts, &t, &s);
        #[cfg(test)]
        let row = if self.is_mutant(crate::Mutant::ContentEquality)
            && op.state == OpState::Staged
            && mode == FsWriteMode::CreateOnly
            && s == Slot::Absent
            && t.digest() == Some(op.content_digest.as_str())
        {
            "R3"
        } else {
            row
        };
        let Some((evidence, result, after, remove)) = plan(row) else {
            return Ok("R9");
        };
        let reason = if row == "R1c" { STAGE_FAILED } else { row };
        let staged_text = staged.as_ref().map(Identity::text);
        self.jwrite("fs_recover", |tx| {
            tx.resolve(&OutcomeRow {
                op_id: op.op_id.clone(),
                operation_evidence: evidence.into(),
                desired_state: desired_state(&t, staged.as_ref(), &op.content_digest).into(),
                result: result.into(),
                reason: reason.into(),
                target_identity_after: Some(t.text(&self.cfg.fs_uuid)),
                displaced_identity: match (row, &prior) {
                    ("R3x", Prior::File(p)) => Some(p.text()),
                    _ => None,
                },
                boot_id: self.boot_id.clone(),
            })?;
            let current = tx.target(&op.target)?;
            let was_tainted = current.as_ref().is_some_and(|r| r.tainted);
            let reason_before = current.as_ref().and_then(|r| r.taint_reason.clone());
            let last_before = current.and_then(|r| r.last_identity);
            let row_after = match after {
                After::UnblockPrior => TargetRow {
                    target: op.target.clone(),
                    blocked_by: None,
                    tainted: was_tainted,
                    taint_reason: reason_before,
                    last_identity: Some(prior.text()),
                },
                After::UnblockStaged => TargetRow {
                    target: op.target.clone(),
                    blocked_by: None,
                    tainted: was_tainted,
                    taint_reason: reason_before,
                    last_identity: staged_text.clone(),
                },
                After::Blocked => TargetRow {
                    target: op.target.clone(),
                    blocked_by: Some(op.op_id.clone()),
                    tainted: was_tainted,
                    taint_reason: reason_before,
                    last_identity: last_before,
                },
                After::Taint => TargetRow {
                    target: op.target.clone(),
                    blocked_by: None,
                    tainted: true,
                    taint_reason: Some(reason_before.unwrap_or_else(|| row.to_string())),
                    last_identity: last_before,
                },
            };
            tx.put_target(&row_after)?;
            Ok(FsEnd::Commit(()))
        })?;
        // Removal only after the resolution commit, and only of the recorded identity (F5).
        let victim = match (remove, &prior) {
            (Remove::Staged, _) => staged.clone(),
            (Remove::Displaced, Prior::File(p)) => Some(p.clone()),
            _ => None,
        };
        if let Some(id) = victim {
            if s.same(&id) && fsfx::unlink_at(&self.staging, &op.staging_name).is_ok() {
                let _ = self.sync(&self.staging, Step::FsyncStaging, "staging");
            }
        }
        Ok(row)
    }

    /// Garbage collection at open (J-7.5 cleanup rule): a staging name is removed only when
    /// its op's result is `applied` or `failed`, the target is neither tainted nor blocked,
    /// and the file has a recorded staged or displaced identity of that op. Every other file
    /// is reported ([`FsWriteAdapter::gc_kept`]), never removed.
    pub(crate) fn collect_garbage(&self, _guard: &MutexGuard<'_, ()>) -> Result<(), TxFailure> {
        let names = fsfx::list_dir(&self.staging)
            .map_err(|e| crate::effect_failure(&format!("list staging {}", e.code())))?;
        let mut kept = Vec::new();
        let mut removed = false;
        for name in names {
            let recorded = self.jread("fs_gc", |tx| {
                let Some(op) = tx.op_by_staging_name(&name)? else {
                    return Ok(None);
                };
                let Some(outcome) = tx.outcome(&op.op_id)? else {
                    return Ok(None);
                };
                let target = tx.target(&op.target)?;
                let clear = target
                    .as_ref()
                    .is_some_and(|r| !r.tainted && r.blocked_by.is_none());
                #[cfg(test)]
                let clear = clear
                    || (self.is_mutant(crate::Mutant::GcTainted)
                        && target.as_ref().is_some_and(|r| r.blocked_by.is_none()));
                if !clear || !matches!(outcome.result.as_str(), "applied" | "failed") {
                    return Ok(None);
                }
                let ids: Vec<Identity> = [op.staged_identity.clone(), outcome.displaced_identity]
                    .into_iter()
                    .flatten()
                    .filter_map(|t| Identity::parse(&t))
                    .collect();
                Ok(Some(ids))
            })?;
            let removable = match (recorded, self.slot(&self.staging, &name)) {
                (Some(ids), Ok(s)) => s.nlink() == 1 && ids.iter().any(|id| s.same(id)),
                _ => false,
            };
            if removable && fsfx::unlink_at(&self.staging, &name).is_ok() {
                removed = true;
            } else {
                kept.push(name);
            }
        }
        if removed {
            self.sync(&self.staging, Step::FsyncStaging, "staging")?;
        }
        *self.gc_kept.lock().unwrap_or_else(PoisonError::into_inner) = kept;
        Ok(())
    }
}
