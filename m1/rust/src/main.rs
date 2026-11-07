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

fn envelope_invalid(id: &str) -> Value {
    obj(vec![("id", s(id)), ("output", obj(vec![("error", s("ENVELOPE_INVALID"))]))])
}

/// Answer one envelope line; `None` when the line has no readable id.
///
/// The envelope is parsed with the strict rules. It must be an object with
/// a string `id` and exactly one of `input` (any JSON value) or `input_text`
/// (a string), and nothing else; otherwise, if a string `id` can still be
/// read, the answer is `ENVELOPE_INVALID`. The `input` value is parsed as its
/// own document, so its depth is counted from its own root exactly as for
/// `input_text`: a depth violation there is the command's `DEPTH_EXCEEDED`
/// (the first problem met reading left to right); any other strict failure
/// inside `input` makes the envelope invalid.
fn answer(command: &str, line: &[u8]) -> Option<Value> {
    let Ok((env, raw)) = json::parse_envelope(line, "input") else {
        return json::recover_string_member(line, "id").map(|id| envelope_invalid(&id));
    };
    let id = env.str_at("id")?.to_string();
    let well_formed = env.as_obj().is_some_and(|m| m.iter().all(|(k, _)| matches!(k.as_str(), "id" | "input" | "input_text")));
    let parsed = match (raw, env.get("input_text")) {
        (Some(raw), None) if well_formed => match json::parse(raw) {
            Err(e) if e != JsonError::DepthExceeded => return Some(envelope_invalid(&id)),
            r => r,
        },
        (None, Some(Value::Str(text))) if well_formed => json::parse(text.as_bytes()),
        _ => return Some(envelope_invalid(&id)),
    };
    let output = match parsed {
        Ok(input) => run_command(command, &input),
        Err(e) => strict_failure(command, e),
    };
    Some(obj(vec![("id", s(&id)), ("output", output)]))
}

/// Whitespace-only lines (JSON whitespace) are ignored.
fn is_blank(line: &[u8]) -> bool {
    line.iter().all(|c| matches!(c, b' ' | b'\t' | b'\r'))
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
        if is_blank(&line) {
            continue;
        }
        match answer(command, &line) {
            Some(v) => {
                if writeln!(out, "{}", json::to_text(&v)).is_err() {
                    return ExitCode::from(1);
                }
            }
            None => {
                eprintln!("line {}: no readable id; not answered", n + 1);
                all_answered = false;
            }
        }
    }
    if out.flush().is_err() {
        return ExitCode::from(1);
    }
    if all_answered { ExitCode::SUCCESS } else { ExitCode::from(1) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn out(command: &str, line: &[u8]) -> Option<String> {
        answer(command, line).map(|v| json::to_text(&v))
    }

    #[test]
    fn envelope_invalid_when_id_is_readable() {
        let invalid = Some(r#"{"id":"e","output":{"error":"ENVELOPE_INVALID"}}"#.to_string());
        for line in [
            &br#"{"id":"e","input":NaN}"#[..],
            br#"{"id":"e","input":{"a":1,"a":2}}"#,
            br#"{"id":"e","input":1,"input_text":"1"}"#,
            br#"{"id":"e"}"#,
            br#"{"id":"e","input_text":1}"#,
            br#"{"id":"e","input_text":"1","x":1}"#,
            br#"{"id":"e","input_text":"\ud800"}"#,
            b"{\"id\":\"e\",\"input_text\":\"\xff\xfe\"}",
        ] {
            assert_eq!(out("canonical", line), invalid, "{}", String::from_utf8_lossy(line));
        }
    }

    #[test]
    fn envelope_without_readable_id_is_not_answered() {
        for line in [&b"[1,2]"[..], b"not json", br#"{"input_text":"1"}"#, br#"{"id":7,"input_text":"1"}"#, br#"{"id":"e","id":"f","input_text":"1"}"#] {
            assert_eq!(out("canonical", line), None, "{}", String::from_utf8_lossy(line));
        }
    }

    #[test]
    fn input_strict_failures_stay_command_level() {
        assert_eq!(out("canonical", br#"{"id":"e","input_text":"[1.0]"}"#).unwrap(), r#"{"id":"e","output":{"error":"NUMBER_NOT_INTEGER"}}"#);
        let deep = format!(r#"{{"id":"e","input":{}{}}}"#, "[".repeat(66), "]".repeat(66));
        assert_eq!(out("canonical", deep.as_bytes()).unwrap(), r#"{"id":"e","output":{"error":"DEPTH_EXCEEDED"}}"#);
        let a = answer("canonical", br#"{"input":{"b":1,"a":2},"id":"e"}"#).unwrap();
        assert_eq!(a.get("output").and_then(|o| o.str_at("canonical")), Some(r#"{"a":2,"b":1}"#));
    }

    #[test]
    fn blank_lines() {
        assert!(is_blank(b"  \t\r") && is_blank(b"") && !is_blank(b" x"));
    }
}
