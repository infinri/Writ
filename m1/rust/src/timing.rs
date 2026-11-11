//! Per-operation broker timing (`bench-broker-scenario`, M1-06d,
//! m1/fixtures/broker/LEDGER-SQLITE.md "Per-operation timing"). Pure
//! observation: monotonic `std::time::Instant` readings recorded next to the
//! code they bracket; nothing here influences a result.

use crate::json::{obj, s, Value};
use std::time::Instant;

/// Nanoseconds since `t` (saturating to the JSON integer range).
pub fn since(t: Instant) -> i64 {
    between(t, Instant::now())
}

/// Nanoseconds from `a` to `b` (0 if `b` is not later), saturating like
/// `since`. Two readings on one clock give exact sums: `between(a, b) +
/// between(b, c) == between(a, c)` for `a <= b <= c`.
pub fn between(a: Instant, b: Instant) -> i64 {
    i64::try_from(b.saturating_duration_since(a).as_nanos()).unwrap_or(i64::MAX).min(crate::json::MAX_SAFE)
}

/// Where a transaction's database error happened (D-026).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TxAt {
    Begin,
    Body,
    End,
}

/// A transaction's database error (D-026): `busy` iff the SQLite primary
/// result code is SQLITE_BUSY, `other` for any other database or integrity
/// error.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TxError {
    pub busy: bool,
    pub at: TxAt,
}

/// One logical transaction. `begin_ns` and `end_ns` are `None` for the
/// in-memory ledger, and so is `error` (always).
#[derive(Clone, Copy, Debug)]
pub struct TxTime {
    pub begin_ns: Option<i64>,
    pub body_ns: i64,
    pub end_ns: Option<i64>,
    pub commit: bool,
    pub error: Option<TxError>,
}

impl TxError {
    pub fn to_value(self) -> Value {
        let at = match self.at {
            TxAt::Begin => "begin",
            TxAt::Body => "body",
            TxAt::End => "end",
        };
        obj(vec![("code", s(if self.busy { "busy" } else { "other" })), ("at", s(at))])
    }
}

/// One step; `operations` is `Some` only for a `concurrent` step.
#[derive(Clone, Debug)]
pub struct StepTiming {
    pub op: String,
    pub total_ns: i64,
    pub validate_ns: Option<i64>,
    pub decide_ns: Option<i64>,
    pub key_lock_wait_ns: Option<i64>,
    /// Transactions in the order they ran.
    pub tx: Vec<(&'static str, TxTime)>,
    pub operations: Option<Vec<StepTiming>>,
    /// `concurrent` step only (D-028): written whenever `operations` is.
    pub group: GroupTiming,
    /// Worker of a `concurrent` group only (D-028).
    pub worker: Option<WorkerTiming>,
}

/// D-028 members of a `concurrent` step (LEDGER-SQLITE.md "Concurrency
/// profiles and release protocol"); offsets from the group's dispatch.
#[derive(Clone, Copy, Debug, Default)]
pub struct GroupTiming {
    pub ready_ns: i64,
    pub release_ns: i64,
    pub start_skew_ns: i64,
}

/// D-028 members of a worker; `release_delay_ns` is `start_ns - release_ns`
/// of its group.
#[derive(Clone, Copy, Debug)]
pub struct WorkerTiming {
    /// `None` for the in-memory ledger.
    pub setup_ns: Option<i64>,
    pub ready_at_ns: i64,
    pub start_ns: i64,
    pub release_delay_ns: i64,
}

impl StepTiming {
    pub fn new(op: Option<&str>) -> StepTiming {
        StepTiming {
            op: op.unwrap_or("unknown").to_string(),
            total_ns: 0,
            validate_ns: None,
            decide_ns: None,
            key_lock_wait_ns: None,
            tx: Vec::new(),
            operations: None,
            group: GroupTiming::default(),
            worker: None,
        }
    }

    /// Time one in-memory ledger section; `f` answers (kept, value).
    pub fn memory<T>(&mut self, name: &'static str, f: impl FnOnce() -> (bool, T)) -> T {
        let t = Instant::now();
        let (commit, v) = f();
        self.tx.push((name, TxTime { begin_ns: None, body_ns: since(t), end_ns: None, commit, error: None }));
        v
    }

    pub fn to_value(&self) -> Value {
        let opt = |v: Option<i64>| v.map_or(Value::Null, Value::Int);
        let tx = self
            .tx
            .iter()
            .map(|(name, t)| {
                let v = obj(vec![
                    ("begin_ns", opt(t.begin_ns)),
                    ("body_ns", Value::Int(t.body_ns)),
                    ("end_ns", opt(t.end_ns)),
                    ("outcome", s(if t.commit { "commit" } else { "rollback" })),
                    ("error", t.error.map_or(Value::Null, TxError::to_value)),
                ]);
                (name.to_string(), v)
            })
            .collect();
        let mut members = vec![
            ("op", s(&self.op)),
            ("total_ns", Value::Int(self.total_ns)),
            ("validate_ns", opt(self.validate_ns)),
            ("decide_ns", opt(self.decide_ns)),
            ("key_lock_wait_ns", opt(self.key_lock_wait_ns)),
            ("tx", Value::Obj(tx)),
        ];
        if let Some(ops) = &self.operations {
            members.push(("ready_ns", Value::Int(self.group.ready_ns)));
            members.push(("release_ns", Value::Int(self.group.release_ns)));
            members.push(("start_skew_ns", Value::Int(self.group.start_skew_ns)));
            members.push(("operations", Value::Arr(ops.iter().map(StepTiming::to_value).collect())));
        }
        if let Some(w) = self.worker {
            members.push(("setup_ns", opt(w.setup_ns)));
            members.push(("ready_at_ns", Value::Int(w.ready_at_ns)));
            members.push(("start_ns", Value::Int(w.start_ns)));
            members.push(("release_delay_ns", Value::Int(w.release_delay_ns)));
        }
        obj(members)
    }
}

/// One scenario (`parse_ns` is added by the caller, which does the parsing).
#[derive(Clone, Debug, Default)]
pub struct ScenarioTiming {
    pub open_ns: Option<i64>,
    pub steps: Vec<StepTiming>,
    pub final_ns: Option<i64>,
}

impl ScenarioTiming {
    pub fn to_value(&self, parse_ns: i64, run_ns: i64) -> Value {
        let opt = |v: Option<i64>| v.map_or(Value::Null, Value::Int);
        obj(vec![
            ("parse_ns", Value::Int(parse_ns)),
            ("open_ns", opt(self.open_ns)),
            ("steps", Value::Arr(self.steps.iter().map(StepTiming::to_value).collect())),
            ("final_ns", opt(self.final_ns)),
            ("run_ns", Value::Int(run_ns)),
        ])
    }
}
