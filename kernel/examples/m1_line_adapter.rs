//! M2-06 differential adapter (development only; a Cargo example, never shipped).
//!
//! Speaks the M1 line protocol (m1/fixtures/M1-PARITY-SUITE.md, D-021) around the kernel's pure
//! command layer so the M1 parity driver and property generators can compare `writ-kernel`
//! with the M1 prototypes. Envelope handling is a protocol concern, not kernel behaviour:
//! a line that is not strict JSON, or whose root is not an object with one string `id`,
//! answers `{"id": null, "output": {"error": "ENVELOPE_INVALID"}}`; a strict line with a
//! string `id` that is not a closed envelope answers `ENVELOPE_INVALID` under that id.
//!
//! Known limitation (recorded in kernel/FIXTURE-MAP.md): an object `input` member is
//! re-serialized and re-parsed instead of being re-parsed from its raw bytes, so a depth
//! violation that only exists inside `input` is reported as an envelope failure here.
//!
//!   cargo run --release --example m1_line_adapter -- decide < lines.jsonl

use std::io::{self, BufRead, Write};

use writ_kernel::{commands, json};

fn envelope_invalid(id: Option<&str>) -> String {
    let id = id.map_or_else(|| "null".to_string(), |s| json::s(s).to_json_text());
    format!("{{\"id\":{id},\"output\":{{\"error\":\"ENVELOPE_INVALID\"}}}}")
}

fn answer(command: &str, line: &[u8]) -> String {
    let Ok(env) = json::parse(line) else {
        return envelope_invalid(None);
    };
    let Some(map) = env.as_obj() else {
        return envelope_invalid(None);
    };
    let Some(json::Value::Str(id)) = map.get("id") else {
        return envelope_invalid(None);
    };
    let closed = map
        .keys()
        .all(|k| matches!(k.as_str(), "id" | "input" | "input_text"));
    let input: Vec<u8> = match (map.get("input"), map.get("input_text")) {
        (Some(v), None) if closed => v.to_json_text().into_bytes(),
        (None, Some(json::Value::Str(text))) if closed => text.clone().into_bytes(),
        _ => return envelope_invalid(Some(id)),
    };
    match commands::run(command, &input) {
        Some(out) => format!("{{\"id\":{},\"output\":{out}}}", json::s(id).to_json_text()),
        None => envelope_invalid(Some(id)),
    }
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = match args.first().map(String::as_str) {
        Some("decide-batch") => "decide",
        Some(c @ ("canonical" | "decide" | "check-acceptance" | "external-refusal")) => c,
        _ => {
            eprintln!("usage: m1_line_adapter canonical|decide|decide-batch|check-acceptance|external-refusal");
            return std::process::ExitCode::from(2);
        }
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
        if writeln!(out, "{}", answer(command, &line)).is_err() {
            return std::process::ExitCode::from(1);
        }
    }
    if out.flush().is_err() {
        return std::process::ExitCode::from(1);
    }
    std::process::ExitCode::SUCCESS
}
