//! M3-08 closure adapter (development only; a Cargo example, never shipped).
//!
//! Speaks the M1 line protocol (D-021; m1/driver/parity.py) for the frozen
//! `broker-scenario` and `transition-scenario` sets and drives the production
//! `writ-broker` `Broker` (execute, approvals, present_permit, reconcile, promote,
//! administer) on a fresh production SQLite ledger and effect store per case
//! (`writ-ledger-sqlite`), under D-037 (items 1, 2, 3, 5, 6, 7). Bindings and harness
//! transformations: m3/closure/bindings.md; classification: m3/closure/check.py.
//!
//! **Test-only authorities.** Every key, channel, approval authority, operator
//! authority, snapshot store, acceptance source, registry entry and attestation here is a
//! test stand-in. None establishes production authentication: human approval, operator
//! authentication, key provisioning, the acceptance service, the runner and the immutable
//! snapshot store remain open obligations (M4..M6). Seeded transition cases are
//! production-component tests with synthetic preconditions.
//!
//! **Harness compositions (D-037 item 7):** C1 ingress refusal audited through
//! `LedgerStore::record_refusal`; C2 approval requests sealed with the fixture ingress
//! key; C3 a synthetic approval authority whose human claim tests the self-approval
//! defense only; C4 partial transaction unavailability (lifecycle transactions fail at
//! begin, audit and time floor stay available), never a total ledger outage; C5
//! deterministic transaction fault injection plus close and reopen (not SIGKILL); C6
//! effect failure through the controlled effect store; C7 and C8 line-protocol answers.
//! Final state is inspected only after every store of the case is closed.
//!
//! **I-1 adaptation log:** with `WRIT_M3_ADAPTATIONS` set to a path, one JSON line per
//! answered case is appended: `{"command", "case", "adaptations": [sorted labels]}`.
//!
//!   cargo run --release --example m3_line_adapter -- broker-scenario < lines.jsonl

mod broker;
mod common;
mod evidence;
mod transition;

use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, Write};
use std::sync::Arc;

use writ_broker::writ_kernel::json::{self, Value};

use common::{Labels, C8};

fn envelope_invalid(id: Option<&str>) -> String {
    let id = id.map_or_else(|| "null".to_string(), |s| json::s(s).to_json_text());
    format!("{{\"id\":{id},\"output\":{{\"error\":\"ENVELOPE_INVALID\"}}}}")
}

/// The answer line and, when the envelope names a case, its id and adaptations.
fn answer(command: &str, line: &[u8]) -> (String, Option<(String, Arc<Labels>)>) {
    let Ok(env) = json::parse(line) else {
        return (envelope_invalid(None), None);
    };
    let Some(map) = env.as_obj() else {
        return (envelope_invalid(None), None);
    };
    let Some(Value::Str(id)) = map.get("id") else {
        return (envelope_invalid(None), None);
    };
    let labels = Arc::new(Labels::default());
    let closed = map
        .keys()
        .all(|k| matches!(k.as_str(), "id" | "input" | "input_text"));
    let parsed = match (map.get("input"), map.get("input_text")) {
        (Some(v), None) if closed => Ok(v.clone()),
        (None, Some(Value::Str(t))) if closed => json::parse(t.as_bytes()),
        _ => return (envelope_invalid(Some(id)), Some((id.clone(), labels))),
    };
    let out = match parsed {
        Err(e) => {
            labels.add(C8);
            json::obj([
                ("input_error", json::s("INPUT_JSON_INVALID")),
                ("json_error", json::s(e.code())),
            ])
        }
        Ok(input) if command == "broker-scenario" => {
            broker::broker_scenario(&input, labels.clone())
        }
        Ok(input) => transition::transition_scenario(id, &input, labels.clone()),
    };
    (
        format!(
            "{{\"id\":{},\"output\":{}}}",
            json::s(id).to_json_text(),
            out.to_json_text()
        ),
        Some((id.clone(), labels)),
    )
}

fn log_line(command: &str, case: &str, labels: &Labels) -> String {
    let adaptations = labels.list().into_iter().map(json::s).collect();
    json::obj([
        ("adaptations", Value::Arr(adaptations)),
        ("case", json::s(case)),
        ("command", json::s(command)),
    ])
    .to_json_text()
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = match args.first().map(String::as_str) {
        Some(c @ ("broker-scenario" | "transition-scenario")) => c.to_string(),
        _ => {
            eprintln!("usage: m3_line_adapter broker-scenario|transition-scenario");
            return std::process::ExitCode::from(2);
        }
    };
    let mut log: Option<File> = match std::env::var_os("WRIT_M3_ADAPTATIONS") {
        None => None,
        Some(path) => match OpenOptions::new().create(true).append(true).open(path) {
            Ok(f) => Some(f),
            Err(_) => {
                eprintln!("WRIT_M3_ADAPTATIONS: cannot open the adaptation log");
                return std::process::ExitCode::from(1);
            }
        },
    };
    let stdin = io::stdin();
    let mut out = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().split(b'\n') {
        let Ok(mut line) = line else {
            return std::process::ExitCode::from(1);
        };
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if line.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        let (text, case) = answer(&command, &line);
        if let (Some(f), Some((id, labels))) = (log.as_mut(), case) {
            if writeln!(f, "{}", log_line(&command, &id, &labels)).is_err() {
                return std::process::ExitCode::from(1);
            }
        }
        if writeln!(out, "{text}").is_err() {
            return std::process::ExitCode::from(1);
        }
    }
    if out.flush().is_err() {
        return std::process::ExitCode::from(1);
    }
    std::process::ExitCode::SUCCESS
}
