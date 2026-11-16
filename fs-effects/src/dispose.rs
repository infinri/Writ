//! The journal side of the `fs_target_release` disposition (D-046 Q-3; M5-INTERFACES
//! J-11.9). The Operator ceremony (challenge, WebAuthn assertion, `dispositions` ledger row)
//! is not implemented here: it needs the lead-owned ledger schema change; this module gives
//! the ceremony what it binds and the steps the journal owns.
//!
//! - [`FsWriteAdapter::release_context`] (begin and finish, under the adapter-wide lock):
//!   refuses `DISPOSITION_NOT_APPLICABLE` for a target that is neither blocked nor tainted,
//!   and `DISPOSITION_PRECONDITION` when an op on the target is unresolved, the root is
//!   disabled, or the worker-gone evidence is missing (owner epoch fenced or the op not live;
//!   the previous journal owner gone; `writ.service`'s `cgroup.procs` holding only this
//!   process). Otherwise it returns the `writ-fs-target-state/1` digest over the `targets`
//!   row, every op on the target with state, identities and outcome, the root's mark for this
//!   boot, and the T and S observations made under the lock.
//! - [`FsWriteAdapter::apply_release`] (step 2), idempotent per `disposition_id`: if
//!   `target_dispositions` holds the id, nothing is done; otherwise the state digest is
//!   recomputed and must equal the decided one (else `lapsed`, nothing applied), then
//!   `blocked_by` and `tainted` are cleared, `last_identity` set to the identity observed at
//!   begin, and the `target_dispositions` row inserted, in one transaction. Recorded outcomes
//!   and staging files are never touched.
//! - [`FsWriteAdapter::complete_dispositions`] (at open, before any operation): for every
//!   `decided` disposition of the root, present in `target_dispositions` means `applied`;
//!   absent with an equal digest runs step 2 then `applied`; otherwise `lapsed`.

use std::sync::PoisonError;

use writ_broker::refusal::Refusal;
use writ_broker::store::TxFailure;
use writ_broker::writ_kernel::json::{self, Value};
use writ_ledger_sqlite::{DispositionRow, FsEnd, FsTx, FsTxError, OpState, OwnerRecord, TargetRow};

use crate::{FsWriteAdapter, Parent, Slot};

/// The worker-gone evidence stored with a disposition (J-11.9).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerGone {
    /// The journal's current owner epoch.
    pub current_epoch: u64,
    /// Every op on the target with the epoch it was written in.
    pub op_epochs: Vec<(String, u64)>,
    /// The journal owner before this instance.
    pub previous_owner: Option<OwnerRecord>,
    /// That owner no longer runs (pid and start time gone, another boot, or this process).
    pub previous_owner_gone: bool,
    /// `cgroup.procs` of `writ.service` held only this process.
    pub cgroup_only_self: bool,
    /// Current boot id.
    pub boot_id: String,
}

/// What `/proc/<pid>/stat` says about a process (M5a-13 L1).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ProcStart {
    /// The process runs with this start time (clock ticks since boot, never 0 here).
    Running(u64),
    /// No such process: `/proc/<pid>` does not exist.
    NoProcess,
    /// `/proc/<pid>/stat` could not be read or parsed, or its start time is 0: nothing can
    /// be concluded (fail closed).
    Unknown,
}

/// Read the start time of `pid` (three states; module documentation of [`ProcStart`]).
pub(crate) fn proc_start(pid: u32) -> ProcStart {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound || e.raw_os_error() == Some(3) => {
            ProcStart::NoProcess
        }
        Err(_) => ProcStart::Unknown,
        Ok(text) => text
            .rfind(')')
            .and_then(|i| text.get(i + 2..))
            .and_then(|rest| rest.split(' ').nth(19))
            .and_then(|f| f.parse::<u64>().ok())
            .filter(|n| *n != 0)
            .map_or(ProcStart::Unknown, ProcStart::Running),
    }
}

/// Whether the journal owner before this instance is gone (J-11.9 worker-gone evidence;
/// M5a-13 L1: fail closed). Gone: no previous owner; another boot; this same process (same
/// pid and start time; the lead-flagged case of M5a-09, kept and recorded in the evidence);
/// or, with `/proc` readable for this process, no process with that pid or one with another
/// start time (pid reuse). **Not** gone: a recorded start time of 0 (the owner's start was
/// unknown when it opened the journal), an unreadable or unparsable `/proc/<pid>/stat`, or a
/// `/proc` that does not show this process itself (not mounted, or hidden).
pub(crate) fn previous_owner_gone(
    previous: Option<&OwnerRecord>,
    boot_id: &str,
    pid: u32,
    probe: impl Fn(u32) -> ProcStart,
) -> bool {
    let Some(p) = previous else {
        return true;
    };
    if p.boot_id != boot_id {
        return true;
    }
    if p.start == 0 {
        return false;
    }
    let ProcStart::Running(own) = probe(pid) else {
        return false;
    };
    if p.pid == pid {
        return p.start == own;
    }
    match probe(p.pid) {
        ProcStart::NoProcess => true,
        ProcStart::Running(start) => start != p.start,
        ProcStart::Unknown => false,
    }
}

/// What an Operator decides on for one target (J-11.9 binding inputs).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReleaseContext {
    /// `fsroot:<root id>:<length>:<target>` (length-prefixed canonical target key).
    pub target_id: String,
    /// The target key.
    pub target: String,
    /// The journal's change counter at begin (the binding revision).
    pub revision: u64,
    /// The `writ-fs-target-state/1` digest.
    pub state_digest: String,
    /// The target observed at begin (`absent` or an identity with digest); the new
    /// `last_identity` after release.
    pub observed: String,
    /// Staging files of the target's ops that the release leaves in place.
    pub kept_files: Vec<String>,
    /// Worker-gone evidence.
    pub evidence: WorkerGone,
}

/// A decided `fs_target_release` (from the ledger's `dispositions` row).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecidedRelease {
    /// Unique disposition id.
    pub disposition_id: String,
    /// The target key.
    pub target: String,
    /// The decided state digest.
    pub state_digest: String,
    /// The identity observed at begin (new `last_identity`).
    pub new_last_identity: String,
}

/// The result of the journal step or of crash completion.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Completion {
    /// Step 2 committed now.
    Applied,
    /// Step 2 had already committed (found by `disposition_id`); nothing done.
    AlreadyApplied,
    /// The state changed since the decision; never applied.
    Lapsed,
}

/// Final status of a disposition in the ledger (step 3).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DispositionStatus {
    /// `applied`.
    Applied,
    /// `lapsed`.
    Lapsed,
}

/// The ledger side the ceremony will implement (`dispositions` table; lead-owned schema).
pub trait DispositionLedger {
    /// Every `decided` disposition of `root_id`.
    fn decided(&self, root_id: &str) -> Vec<DecidedRelease>;
    /// Record the final status of `disposition_id` (step 3).
    fn set_status(&self, disposition_id: &str, status: DispositionStatus);
}

/// (state digest, T observation text, staging names present).
type TargetState = (String, String, Vec<String>);

fn opt(v: &Option<String>) -> Value {
    v.as_deref().map_or(Value::Null, json::s)
}

impl FsWriteAdapter<'_> {
    /// The state digest of `target`, the T observation text and the staging names present.
    fn target_state(
        &self,
        tx: &FsTx<'_>,
        target: &str,
    ) -> Result<Result<TargetState, Refusal>, FsTxError> {
        let row = tx.target(target)?;
        let ops = tx.ops_for_target(target)?;
        let marked = tx.root_marks()?.iter().any(|(b, _)| *b == self.boot_id);
        let t = match self.target_slot(target) {
            Ok((Parent::Found(..), t)) => t,
            Ok((Parent::Missing, _)) => Slot::Absent,
            _ => return Ok(Err(Refusal::DispositionPrecondition)),
        };
        let observed = t.text(&self.cfg.fs_uuid);
        let mut op_values = Vec::new();
        let mut kept = Vec::new();
        for op in &ops {
            let s = match self.slot(&self.staging, &op.staging_name) {
                Ok(s) => s,
                Err(_) => return Ok(Err(Refusal::DispositionPrecondition)),
            };
            if s != Slot::Absent {
                kept.push(op.staging_name.clone());
            }
            let outcome = tx.outcome(&op.op_id)?;
            op_values.push(json::obj([
                ("op_id", json::s(&op.op_id)),
                ("state", json::s(op.state.as_str())),
                ("expected_prior", json::s(&op.expected_prior)),
                ("staged_identity", opt(&op.staged_identity)),
                (
                    "owner_epoch",
                    Value::Int(i64::try_from(op.owner_epoch).unwrap_or(-1)),
                ),
                ("result", opt(&outcome.as_ref().map(|o| o.result.clone()))),
                ("reason", opt(&outcome.map(|o| o.reason))),
                ("staging", json::s(&s.text(&self.cfg.fs_uuid))),
            ]));
        }
        let doc = json::obj([
            ("contract", json::s("writ-fs-target-state/1")),
            ("root_id", json::s(&self.cfg.root_id)),
            ("target", json::s(target)),
            (
                "blocked_by",
                opt(&row.as_ref().and_then(|r| r.blocked_by.clone())),
            ),
            (
                "tainted",
                Value::Bool(row.as_ref().is_some_and(|r| r.tainted)),
            ),
            (
                "taint_reason",
                opt(&row.as_ref().and_then(|r| r.taint_reason.clone())),
            ),
            ("last_identity", opt(&row.and_then(|r| r.last_identity))),
            ("ops", Value::Arr(op_values)),
            ("root_marked_this_boot", Value::Bool(marked)),
            ("observed", json::s(&observed)),
        ]);
        let digest = json::digest(&doc).map_err(|_| FsTxError::integrity())?;
        Ok(Ok((digest.as_str().to_string(), observed, kept)))
    }

    fn worker_gone(&self, ops: &[(String, u64, OpState)]) -> WorkerGone {
        let pid = std::process::id();
        let previous = self.journal.previous_owner().cloned();
        let previous_owner_gone =
            previous_owner_gone(previous.as_ref(), &self.boot_id, pid, proc_start);
        let cgroup_only_self = self
            .cfg
            .service_cgroup_procs
            .as_ref()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .is_some_and(|text| {
                let pids: Vec<&str> = text.split_whitespace().collect();
                pids == [pid.to_string().as_str()]
            });
        WorkerGone {
            current_epoch: self.journal.epoch(),
            op_epochs: ops.iter().map(|(id, e, _)| (id.clone(), *e)).collect(),
            previous_owner: previous,
            previous_owner_gone,
            cgroup_only_self,
            boot_id: self.boot_id.clone(),
        }
    }

    /// The release context of `target` (begin, and again at finish; module documentation).
    pub fn release_context(&self, target: &str) -> Result<ReleaseContext, Refusal> {
        let _guard = self.lock();
        if self.is_disabled() {
            return Err(Refusal::DispositionPrecondition);
        }
        let unavailable = |f: TxFailure| Refusal::LedgerUnavailable(f.error);
        let read = self
            .jread("fs_release_context", |tx| {
                let Some(row) = tx.target(target)? else {
                    return Ok(Err(Refusal::DispositionNotApplicable));
                };
                if row.blocked_by.is_none() && !row.tainted {
                    return Ok(Err(Refusal::DispositionNotApplicable));
                }
                let ops: Vec<(String, u64, OpState)> = tx
                    .ops_for_target(target)?
                    .into_iter()
                    .map(|o| (o.op_id, o.owner_epoch, o.state))
                    .collect();
                let revision = tx.change_counter()?;
                let state = self.target_state(tx, target)?;
                Ok(Ok((ops, revision, state)))
            })
            .map_err(unavailable)??;
        let (ops, revision, state) = read;
        let (state_digest, observed, kept_files) = state?;
        let live = self
            .live
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let evidence = self.worker_gone(&ops);
        let fenced = ops.iter().all(|(id, epoch, st)| {
            *st == OpState::Resolved && (*epoch < evidence.current_epoch || !live.contains(id))
        });
        if !fenced || !evidence.previous_owner_gone || !evidence.cgroup_only_self {
            return Err(Refusal::DispositionPrecondition);
        }
        Ok(ReleaseContext {
            target_id: format!("fsroot:{}:{}:{target}", self.cfg.root_id, target.len()),
            target: target.to_string(),
            revision,
            state_digest,
            observed,
            kept_files,
            evidence,
        })
    }

    /// Step 2 of J-11.9 under the adapter-wide lock (module documentation).
    pub fn apply_release(&self, d: &DecidedRelease) -> Result<Completion, TxFailure> {
        let _guard = self.lock();
        if self.is_disabled() {
            return Err(Self::unrecoverable());
        }
        self.jwrite("fs_release", |tx| {
            if tx.disposition(&d.disposition_id)?.is_some() {
                return Ok(FsEnd::Rollback(Completion::AlreadyApplied));
            }
            let current = match self.target_state(tx, &d.target)? {
                Ok((digest, observed, _)) => (digest, observed),
                Err(_) => return Ok(FsEnd::Rollback(Completion::Lapsed)),
            };
            if current.0 != d.state_digest || current.1 != d.new_last_identity {
                return Ok(FsEnd::Rollback(Completion::Lapsed));
            }
            let row = tx.target(&d.target)?.ok_or_else(FsTxError::integrity)?;
            tx.put_target(&TargetRow {
                target: d.target.clone(),
                blocked_by: None,
                tainted: false,
                taint_reason: None,
                last_identity: Some(d.new_last_identity.clone()),
            })?;
            tx.insert_disposition(&DispositionRow {
                disposition_id: d.disposition_id.clone(),
                target: d.target.clone(),
                state_digest: d.state_digest.clone(),
                prev_blocked_by: row.blocked_by,
                prev_tainted: row.tainted,
                prev_taint_reason: row.taint_reason,
                prev_last_identity: row.last_identity,
                new_last_identity: d.new_last_identity.clone(),
            })?;
            Ok(FsEnd::Commit(Completion::Applied))
        })
    }

    /// Completion after a crash (J-11.9), for every `decided` disposition of this root.
    pub fn complete_dispositions(
        &self,
        ledger: &dyn DispositionLedger,
    ) -> Result<Vec<(String, Completion)>, TxFailure> {
        let mut out = Vec::new();
        for d in ledger.decided(&self.cfg.root_id) {
            let done = self.apply_release(&d)?;
            ledger.set_status(
                &d.disposition_id,
                match done {
                    Completion::Applied | Completion::AlreadyApplied => DispositionStatus::Applied,
                    Completion::Lapsed => DispositionStatus::Lapsed,
                },
            );
            out.push((d.disposition_id, done));
        }
        Ok(out)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn owner(pid: u32, start: u64, boot: &str) -> OwnerRecord {
        OwnerRecord {
            pid,
            start,
            boot_id: boot.into(),
        }
    }

    /// M5a-13 L1: an unreadable `/proc/<pid>/stat` or a start time of 0 is not "gone".
    #[test]
    fn previous_owner_gone_fails_closed() {
        let me = 100;
        let probe = |table: Vec<(u32, ProcStart)>| {
            move |pid: u32| {
                table
                    .iter()
                    .find(|(p, _)| *p == pid)
                    .map_or(ProcStart::NoProcess, |(_, s)| *s)
            }
        };
        let normal = || {
            probe(vec![
                (me, ProcStart::Running(500)),
                (7, ProcStart::Running(9)),
            ])
        };
        assert!(previous_owner_gone(None, "b", me, normal()));
        assert!(previous_owner_gone(
            Some(&owner(7, 9, "old")),
            "b",
            me,
            normal()
        ));
        // Running with the recorded start time: not gone.
        assert!(!previous_owner_gone(
            Some(&owner(7, 9, "b")),
            "b",
            me,
            normal()
        ));
        // Pid reused (another start time) or no such process: gone.
        assert!(previous_owner_gone(
            Some(&owner(7, 8, "b")),
            "b",
            me,
            normal()
        ));
        assert!(previous_owner_gone(
            Some(&owner(8, 8, "b")),
            "b",
            me,
            normal()
        ));
        // This same process.
        assert!(previous_owner_gone(
            Some(&owner(me, 500, "b")),
            "b",
            me,
            normal()
        ));
        assert!(!previous_owner_gone(
            Some(&owner(me, 499, "b")),
            "b",
            me,
            normal()
        ));
        // Fail closed: recorded start 0, unreadable stat, /proc not showing this process.
        assert!(!previous_owner_gone(
            Some(&owner(8, 0, "b")),
            "b",
            me,
            normal()
        ));
        assert!(!previous_owner_gone(
            Some(&owner(me, 0, "b")),
            "b",
            me,
            normal()
        ));
        let unreadable = probe(vec![(me, ProcStart::Running(500)), (7, ProcStart::Unknown)]);
        assert!(!previous_owner_gone(
            Some(&owner(7, 9, "b")),
            "b",
            me,
            unreadable
        ));
        let no_proc = probe(vec![]);
        assert!(!previous_owner_gone(
            Some(&owner(7, 9, "b")),
            "b",
            me,
            no_proc
        ));
        // The real reader: this process runs; a pid beyond pid_max does not exist.
        assert!(matches!(proc_start(std::process::id()), ProcStart::Running(n) if n > 0));
        assert_eq!(proc_start(u32::MAX), ProcStart::NoProcess);
    }
}
