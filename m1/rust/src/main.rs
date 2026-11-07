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
    let output = match parsed {
        Ok(input) => run_command(command, &input),
        Err(e) => strict_failure(command, e),
    };
    obj(vec![("id", s(id)), ("output", output)])
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
