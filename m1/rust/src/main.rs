//! `proto <command>`: JSON Lines protocol of the M1 parity suite
//! (docs/implementation/M1-PARITY-SUITE.md, "Prototype interface").
//!
//! Every line is answered in input order; refusals are outputs. Each input
//! (`input` or `input_text`) is parsed by the strict parser in `json.rs`.

mod acceptance;
mod broker;
mod ids;
mod json;
mod kernel;
mod promotion;
mod refusal;
mod sqlite;
mod time;
mod timing;

use json::{obj, s, JsonError, Value};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::OnceLock;
use std::time::Instant;

const COMMANDS: [&str; 9] = [
    "canonical",
    "decide",
    "decide-batch",
    "bench-decide",
    "check-acceptance",
    "external-refusal",
    "transition-scenario",
    "broker-scenario",
    "bench-broker-scenario",
];

/// The `broker-scenario` and `bench-broker-scenario` ledger, chosen once
/// from the command line.
static LEDGER: OnceLock<broker::LedgerChoice> = OnceLock::new();

/// `broker-scenario` options (LEDGER-SQLITE.md "Selecting the ledger"): none,
/// or `--ledger sqlite` with exactly one of `--ledger-dir DIR` and
/// `--ledger-db PATH`, each at most once, in any order.
fn ledger_choice(options: &[String]) -> Option<broker::LedgerChoice> {
    let (mut ledger, mut dir, mut db) = (None, None, None);
    for pair in options.chunks(2) {
        let [name, value] = pair else { return None };
        let slot = match name.as_str() {
            "--ledger" => &mut ledger,
            "--ledger-dir" => &mut dir,
            "--ledger-db" => &mut db,
            _ => return None,
        };
        if slot.replace(value.clone()).is_some() {
            return None;
        }
    }
    match (ledger.as_deref(), dir, db) {
        (None, None, None) => Some(broker::LedgerChoice::Memory),
        (Some("sqlite"), Some(d), None) => Some(broker::LedgerChoice::SqliteDir(PathBuf::from(d))),
        (Some("sqlite"), None, Some(p)) => Some(broker::LedgerChoice::SqliteDb(PathBuf::from(p))),
        _ => None,
    }
}

fn strict_failure(command: &str, e: JsonError) -> Value {
    let code = s(e.code());
    match command {
        "canonical" => obj(vec![("error", code)]),
        "decide" => obj(vec![
            ("status", s("invalid")),
            ("reason_code", s("REQUEST_JSON_INVALID")),
            ("matched_rule_ids", Value::Arr(vec![])),
        ]),
        "check-acceptance" => obj(vec![("refusal", s("INPUT_JSON_INVALID")), ("json_error", code)]),
        _ => obj(vec![("input_error", s("INPUT_JSON_INVALID")), ("json_error", code)]),
    }
}

fn run_command(command: &str, input: &Value) -> Value {
    match command {
        "canonical" => match (json::canonical(input), json::digest(input)) {
            (Ok(text), Ok(d)) => obj(vec![("canonical", s(&text)), ("digest", s(&d))]),
            _ => obj(vec![("error", s("DEPTH_EXCEEDED"))]),
        },
        "decide" => kernel::decide_command(input),
        "check-acceptance" => match acceptance::check(input.get("record"), input.get("current"), input.get("requirements")) {
            Ok(()) => obj(vec![("ok", Value::Bool(true))]),
            Err(code) => obj(vec![("refusal", s(code))]),
        },
        "external-refusal" => obj(vec![("external", s(refusal::external(input.str_at("internal_code").unwrap_or(""))))]),
        "transition-scenario" => promotion::run(input),
        "broker-scenario" => broker::run_with(input, LEDGER.get().unwrap_or(&broker::LedgerChoice::Memory)),
        _ => unreachable!(),
    }
}

/// `id` is `Some` only when the line parsed strictly with exactly one
/// string `id` member; otherwise it is `null` (D-021: never recovered or
/// fabricated).
fn envelope_invalid(id: Option<&str>) -> Value {
    obj(vec![("id", id.map_or(Value::Null, s)), ("output", obj(vec![("error", s("ENVELOPE_INVALID"))]))])
}

/// Answer one envelope line (D-021). Every line gets exactly one answer.
///
/// The line is parsed with the strict rules; the raw bytes of a root `input`
/// member are then parsed as their own document, so its depth is counted from
/// its own root exactly as for `input_text`: a depth violation there is the
/// command's `DEPTH_EXCEEDED`, and any other strict failure inside `input`
/// means the line is not strict JSON. A line that is not strict JSON, or
/// whose root is not an object with one string `id`, is answered with a
/// `null` id. A strict line with a string `id` that is not a closed envelope
/// (other members, both or neither payload member, non-string `input_text`)
/// is `ENVELOPE_INVALID` under that id. No command runs for an invalid
/// envelope.
fn answer(command: &str, line: &[u8]) -> Value {
    let started = Instant::now();
    let Ok((env, raw)) = json::parse_envelope(line, "input") else {
        return envelope_invalid(None);
    };
    let parsed_input = raw.map(json::parse);
    if matches!(parsed_input, Some(Err(e)) if e != JsonError::DepthExceeded) {
        return envelope_invalid(None);
    }
    let Some(id) = env.str_at("id") else {
        return envelope_invalid(None);
    };
    let closed = env.as_obj().is_some_and(|m| m.iter().all(|(k, _)| matches!(k.as_str(), "id" | "input" | "input_text")));
    let parsed = match (parsed_input, env.get("input_text")) {
        (Some(r), None) if closed => r,
        (None, Some(Value::Str(text))) if closed => json::parse(text.as_bytes()),
        _ => return envelope_invalid(Some(id)),
    };
    let parse_ns = timing::since(started);
    obj(vec![("id", s(id)), ("output", execute(command, parsed, parse_ns))])
}

/// `decide-batch` is `decide` (workload A). `bench-decide` (M1-06a) answers
/// `{"decision": <the decide output>, "kernel_ns": n}`, where `n` is the
/// monotonic time spent producing the decision from the already parsed input,
/// excluding envelope and input parsing, output serialization and I/O.
/// `bench-broker-scenario` (M1-06d) answers `{"scenario": S, "timing": T}`
/// (LEDGER-SQLITE.md "Per-operation timing"): `parse_ns` is the strict
/// parsing of the line, `run_ns` runs from just after parsing until `S` is
/// complete.
fn execute(command: &str, parsed: Result<Value, JsonError>, parse_ns: i64) -> Value {
    let decide = |parsed: Result<Value, JsonError>| match parsed {
        Ok(input) => run_command("decide", &input),
        Err(e) => strict_failure("decide", e),
    };
    match command {
        "decide-batch" => decide(parsed),
        "bench-decide" => {
            let started = Instant::now();
            let decision = decide(parsed);
            let ns = i64::try_from(started.elapsed().as_nanos()).unwrap_or(i64::MAX);
            obj(vec![("decision", decision), ("kernel_ns", Value::Int(ns))])
        }
        "bench-broker-scenario" => {
            let started = Instant::now();
            let (scenario, t) = match parsed {
                Ok(input) => broker::run_timed(&input, LEDGER.get().unwrap_or(&broker::LedgerChoice::Memory)),
                Err(e) => (strict_failure("broker-scenario", e), timing::ScenarioTiming::default()),
            };
            let run_ns = timing::since(started);
            obj(vec![("scenario", scenario), ("timing", t.to_value(parse_ns, run_ns))])
        }
        _ => match parsed {
            Ok(input) => run_command(command, &input),
            Err(e) => strict_failure(command, e),
        },
    }
}

/// Whitespace-only lines (JSON whitespace) are ignored.
fn is_blank(line: &[u8]) -> bool {
    line.iter().all(|c| matches!(c, b' ' | b'\t' | b'\r'))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let usage = || {
        eprintln!("usage: proto <{}> | proto (broker-scenario|bench-broker-scenario) [--ledger sqlite (--ledger-dir DIR | --ledger-db PATH)] | proto sqlite-info", COMMANDS.join("|"));
        ExitCode::from(2)
    };
    let options = args.get(2..).unwrap_or_default();
    if args.get(1).map(String::as_str) == Some("sqlite-info") && options.is_empty() {
        return match sqlite::info() {
            Ok(info) => {
                println!("{}", json::to_text(&info));
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("proto: sqlite-info: {}", e.0);
                ExitCode::from(1)
            }
        };
    }
    let Some(command) = args.get(1).filter(|c| COMMANDS.contains(&c.as_str())) else {
        return usage();
    };
    let choice = if matches!(command.as_str(), "broker-scenario" | "bench-broker-scenario") { ledger_choice(options) } else { options.is_empty().then_some(broker::LedgerChoice::Memory) };
    let Some(choice) = choice else {
        return usage();
    };
    let _ = LEDGER.set(choice);
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().split(b'\n') {
        let Ok(mut line) = line else {
            return ExitCode::from(1);
        };
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if is_blank(&line) {
            continue;
        }
        if writeln!(out, "{}", json::to_text(&answer(command, &line))).is_err() {
            return ExitCode::from(1);
        }
    }
    if out.flush().is_err() {
        return ExitCode::from(1);
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn out(command: &str, line: &[u8]) -> String {
        json::to_text(&answer(command, line))
    }

    #[test]
    fn strict_envelope_with_string_id_keeps_id() {
        let invalid = r#"{"id":"e","output":{"error":"ENVELOPE_INVALID"}}"#;
        for line in [
            &br#"{"id":"e","input":1,"input_text":"1"}"#[..],
            br#"{"id":"e"}"#,
            br#"{"id":"e","input_text":1}"#,
            br#"{"id":"e","input_text":"1","x":1}"#,
            br#"{"x":null,"input":1,"id":"e"}"#,
            br#"{"id":"e","input_text":{"a":1}}"#,
        ] {
            assert_eq!(out("canonical", line), invalid, "{}", String::from_utf8_lossy(line));
        }
    }

    #[test]
    fn untrusted_identity_is_null() {
        // D-021: no id is recovered from a line that is not strict JSON, and
        // none is fabricated when the strict root has no single string `id`.
        let invalid = r#"{"id":null,"output":{"error":"ENVELOPE_INVALID"}}"#;
        for line in [
            &br#"{"id":"e","input":NaN}"#[..],
            br#"{"id":"e","input":{"a":1,"a":2}}"#,
            br#"{"id":"e","input":[1}"#,
            br#"{"id":"e","input":1,"input":2}"#,
            br#"{"id":"e","input_text":"\ud800"}"#,
            b"{\"id\":\"e\",\"input_text\":\"\xff\xfe\"}",
            br#"{"id":"e","id":"f","input_text":"1"}"#,
            br#"{"id":"e","input_text":"1"} x"#,
            br#"{"id":"e","input_text":"1""#,
            br#"[1,2]"#,
            b"not json",
            br#"{"input_text":"1"}"#,
            br#"{"id":7,"input_text":"1"}"#,
            br#"{"id":null,"input_text":"1"}"#,
            br#""e""#,
        ] {
            assert_eq!(out("canonical", line), invalid, "{}", String::from_utf8_lossy(line));
            assert_eq!(out("decide", line), invalid, "no command runs: {}", String::from_utf8_lossy(line));
        }
    }

    #[test]
    fn decide_strict_failure_has_exactly_three_members() {
        for line in [&br#"{"id":"d","input_text":"{"}"#[..], br#"{"id":"d","input_text":"{\"a\":1,\"a\":1}"}"#] {
            assert_eq!(
                out("decide", line),
                r#"{"id":"d","output":{"matched_rule_ids":[],"reason_code":"REQUEST_JSON_INVALID","status":"invalid"}}"#
            );
        }
    }

    #[test]
    fn decide_harness_fields_refuse_with_three_members() {
        // D-022: malformed `now`, `state_revision` or a missing `request` is
        // refused before any decision, with no placeholder metadata.
        let refusal = r#"{"id":"d","output":{"matched_rule_ids":[],"reason_code":"REQUEST_SCHEMA_INVALID","status":"invalid"}}"#;
        for input in [
            r#"{"bundle":null,"state_revision":1,"request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-02-30T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00+00:00","request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00.1Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":-1,"now":"2026-01-01T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":"1","now":"2026-01-01T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00Z"}"#,
            r#"[]"#,
        ] {
            let line = format!(r#"{{"id":"d","input":{input}}}"#);
            assert_eq!(out("decide", line.as_bytes()), refusal, "{input}");
        }
        let ok = out("decide", br#"{"id":"d","input":{"bundle":null,"state_revision":9007199254740991,"now":"2024-02-29T23:59:59Z","request":{}}}"#);
        assert!(ok.contains(r#""decided_at":"2024-02-29T23:59:59Z""#), "{ok}");
    }

    #[test]
    fn decide_batch_and_bench_decide_return_the_decide_output() {
        for line in [
            &br#"{"id":"d","input_text":"{"}"#[..],
            br#"{"id":"d","input":{"bundle":null,"state_revision":0,"now":"2026-01-01T00:00:00Z","request":{}}}"#,
        ] {
            let plain = answer("decide", line);
            assert_eq!(json::to_text(&answer("decide-batch", line)), json::to_text(&plain));
            let timed = answer("bench-decide", line);
            let out = timed.get("output").unwrap();
            assert_eq!(json::to_text(out.get("decision").unwrap()), json::to_text(plain.get("output").unwrap()));
            assert!(matches!(out.get("kernel_ns"), Some(Value::Int(n)) if *n >= 0));
            assert_eq!(out.as_obj().unwrap().len(), 2);
        }
        let invalid = r#"{"id":"d","output":{"error":"ENVELOPE_INVALID"}}"#;
        assert_eq!(out("bench-decide", br#"{"id":"d"}"#), invalid);
    }

    #[test]
    fn bench_broker_scenario_wraps_the_broker_scenario_output() {
        for line in [&br#"{"id":"b","input_text":"{"}"#[..], br#"{"id":"b","input":{"steps":[]}}"#] {
            let plain = answer("broker-scenario", line);
            let timed = answer("bench-broker-scenario", line);
            let out = timed.get("output").unwrap();
            assert_eq!(json::to_text(out.get("scenario").unwrap()), json::to_text(plain.get("output").unwrap()));
            let t = out.get("timing").unwrap();
            assert_eq!(t.as_obj().unwrap().len(), 5);
            assert!(matches!((t.get("parse_ns"), t.get("run_ns")), (Some(Value::Int(p)), Some(Value::Int(r))) if *p >= 0 && *r >= 0));
            assert_eq!(json::to_text(t.get("steps").unwrap()), "[]");
            assert!(t.get("final_ns").unwrap().is_null() && t.get("open_ns").unwrap().is_null());
        }
    }

    #[test]
    fn input_strict_failures_stay_command_level() {
        assert_eq!(out("canonical", br#"{"id":"e","input_text":"[1.0]"}"#), r#"{"id":"e","output":{"error":"NUMBER_NOT_INTEGER"}}"#);
        let deep = format!(r#"{{"id":"e","input":{}{}}}"#, "[".repeat(66), "]".repeat(66));
        assert_eq!(out("canonical", deep.as_bytes()), r#"{"id":"e","output":{"error":"DEPTH_EXCEEDED"}}"#);
        let a = answer("canonical", br#"{"input":{"b":1,"a":2},"id":"e"}"#);
        assert_eq!(a.get("output").and_then(|o| o.str_at("canonical")), Some(r#"{"a":2,"b":1}"#));
    }

    #[test]
    fn blank_lines() {
        assert!(is_blank(b"  \t\r") && is_blank(b"") && !is_blank(b" x"));
    }
}
