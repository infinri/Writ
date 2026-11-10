//! Per-operation broker timing (`bench-broker-scenario`, M1-06d,
//! m1/fixtures/broker/LEDGER-SQLITE.md "Per-operation timing"). Pure
//! observation: monotonic `std::time::Instant` readings recorded next to the
//! code they bracket; nothing here influences a result.

use crate::json::{obj, s, Value};
use std::time::Instant;

/// Nanoseconds since `t` (saturating to the JSON integer range).
pub fn since(t: Instant) -> i64 {
    i64::try_from(t.elapsed().as_nanos()).unwrap_or(i64::MAX).min(crate::json::MAX_SAFE)
}

/// One logical transaction. `begin_ns` and `end_ns` are `None` for the
/// in-memory ledger.
#[derive(Clone, Copy, Debug)]
pub struct TxTime {
    pub begin_ns: Option<i64>,
    pub body_ns: i64,
    pub end_ns: Option<i64>,
    pub commit: bool,
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
        }
    }

    /// Time one in-memory ledger section; `f` answers (kept, value).
    pub fn memory<T>(&mut self, name: &'static str, f: impl FnOnce() -> (bool, T)) -> T {
        let t = Instant::now();
        let (commit, v) = f();
        self.tx.push((name, TxTime { begin_ns: None, body_ns: since(t), end_ns: None, commit }));
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
            members.push(("operations", Value::Arr(ops.iter().map(StepTiming::to_value).collect())));
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
