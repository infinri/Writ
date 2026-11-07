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
mod time;

use json::{obj, s, JsonError, Value};
use std::io::{self, BufRead, Write};
use std::process::ExitCode;

const COMMANDS: [&str; 6] = ["canonical", "decide", "check-acceptance", "external-refusal", "transition-scenario", "broker-scenario"];

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
        "broker-scenario" => broker::run(input),
        _ => unreachable!(),
    }
}

/// Answer one envelope line; `None` when the line carries no usable id.
fn answer(command: &str, line: &[u8]) -> Option<Value> {
    let (env, raw) = json::parse_envelope(line, "input").ok()?;
    let id = env.str_at("id")?.to_string();
    let parsed = match (raw, env.get("input_text")) {
        (Some(raw), None) => json::parse(raw),
        (None, Some(Value::Str(text))) => json::parse(text.as_bytes()),
        _ => return None,
    };
    let output = match parsed {
        Ok(input) => run_command(command, &input),
        Err(e) => strict_failure(command, e),
    };
    Some(obj(vec![("id", s(&id)), ("output", output)]))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let Some(command) = args.get(1).filter(|c| COMMANDS.contains(&c.as_str())) else {
        eprintln!("usage: proto <{}>", COMMANDS.join("|"));
        return ExitCode::from(2);
    };
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());
    let mut all_answered = true;
    for (n, line) in stdin.lock().split(b'\n').enumerate() {
        let Ok(mut line) = line else {
            all_answered = false;
            break;
        };
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if line.iter().all(|c| c.is_ascii_whitespace()) {
            continue;
        }
        match answer(command, &line) {
            Some(v) => {
                if writeln!(out, "{}", json::to_text(&v)).is_err() {
                    return ExitCode::from(1);
                }
            }
            None => {
                eprintln!("line {}: not a valid envelope with an id; not answered", n + 1);
                all_answered = false;
            }
        }
    }
    if out.flush().is_err() {
        return ExitCode::from(1);
    }
    if all_answered { ExitCode::SUCCESS } else { ExitCode::from(1) }
}
