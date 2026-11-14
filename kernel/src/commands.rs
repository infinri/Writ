//! Pure JSON-text-in, JSON-text-out kernel commands.
//!
//! Each command takes the raw bytes of one JSON text, parses them with the
//! strict parser (D-017) and returns the output JSON text in the compact
//! canonical encoding. No I/O, no clock, no environment: the fixture tests and
//! the differential harness drive the kernel through this layer. The command
//! names and input and output shapes are those of the M1 parity protocol
//! (`docs/implementation/M1-PARITY-SUITE.md`); the line envelope itself is not
//! part of the kernel.

use crate::acceptance;
use crate::contracts::{KernelCode, StateRevision};
use crate::decision::{self, Policy};
use crate::json::{self, obj, s, JsonError, Value};
use crate::policy::load_bundle;
use crate::refusal;
use crate::time::Timestamp;

/// Kernel command names.
pub const COMMANDS: [&str; 4] = [
    "canonical",
    "decide",
    "check-acceptance",
    "external-refusal",
];

/// Run kernel command `command` on `input`; `None` for a name that is not a kernel command.
pub fn run(command: &str, input: &[u8]) -> Option<String> {
    match command {
        "canonical" => Some(canonical(input)),
        "decide" => Some(decide(input)),
        "check-acceptance" => Some(check_acceptance(input)),
        "external-refusal" => Some(external_refusal(input)),
        _ => None,
    }
}

/// `canonical`: `{"canonical": <text>, "digest": "sha256:..."}` or `{"error": <code>}`.
pub fn canonical(input: &[u8]) -> String {
    canonical_value(input).to_json_text()
}

fn canonical_value(input: &[u8]) -> Value {
    let parsed = match json::parse(input) {
        Ok(v) => v,
        Err(e) => return obj([("error", s(e.code()))]),
    };
    match json::canonical(&parsed) {
        Ok(text) => {
            let digest = json::Digest::of_bytes(text.as_bytes());
            obj([("canonical", s(&text)), ("digest", s(digest.as_str()))])
        }
        Err(e) => obj([("error", s(e.code()))]),
    }
}

/// The three-member pre-decision refusal (D-021, D-022): not a Decision contract instance.
fn decide_refusal(code: KernelCode) -> Value {
    obj([
        ("status", s("invalid")),
        ("reason_code", s(code.as_str())),
        ("matched_rule_ids", Value::Arr(Vec::new())),
    ])
}

/// `decide`: input `{"bundle": <bundle> | null, "state_revision": n, "now": "<YYYY-MM-DDTHH:MM:SSZ>", "request": <request>}`.
///
/// Strict-parse failure is the three-member `REQUEST_JSON_INVALID` refusal;
/// a non-object input, an absent `request`, a `now` not in the strict form or
/// a `state_revision` that is not an integer from 0 to 2^53-1 is the
/// three-member `REQUEST_SCHEMA_INVALID` refusal (D-022). Otherwise the output
/// is a Decision; a present but malformed `request` still gets a complete
/// Decision. A `bundle` that is absent or `null` is no policy; any other value
/// that fails to load is a rejected bundle.
pub fn decide(input: &[u8]) -> String {
    decide_value(input).to_json_text()
}

fn decide_value(input: &[u8]) -> Value {
    let parsed = match json::parse(input) {
        Ok(v) => v,
        Err(_) => return decide_refusal(KernelCode::RequestJsonInvalid),
    };
    let now = parsed
        .get("now")
        .and_then(Value::as_str)
        .and_then(Timestamp::parse_strict);
    let revision = parsed
        .get("state_revision")
        .and_then(Value::as_int)
        .and_then(StateRevision::new);
    let (Some(now), Some(revision), Some(request)) = (now, revision, parsed.get("request")) else {
        return decide_refusal(KernelCode::RequestSchemaInvalid);
    };
    let loaded = match parsed.get("bundle") {
        None | Some(Value::Null) => None,
        Some(doc) => Some(load_bundle(doc)),
    };
    let policy = match &loaded {
        None => Policy::Absent,
        Some(Ok(bundle)) => Policy::Loaded(bundle),
        Some(Err(_)) => Policy::Rejected,
    };
    decision::decide(request, policy, revision, now).to_value()
}

fn input_json_invalid(e: JsonError, member: &str) -> Value {
    obj([
        (member, s("INPUT_JSON_INVALID")),
        ("json_error", s(e.code())),
    ])
}

/// `check-acceptance`: input `{"record", "current", "requirements"}`; output
/// `{"ok": true}`, `{"refusal": <code>}` or, on strict-parse failure,
/// `{"refusal": "INPUT_JSON_INVALID", "json_error": <code>}`.
pub fn check_acceptance(input: &[u8]) -> String {
    let out = match json::parse(input) {
        Err(e) => input_json_invalid(e, "refusal"),
        Ok(v) => {
            match acceptance::check_values(v.get("record"), v.get("current"), v.get("requirements"))
            {
                Ok(()) => obj([("ok", Value::Bool(true))]),
                Err(r) => obj([("refusal", s(r.code()))]),
            }
        }
    };
    out.to_json_text()
}

/// `external-refusal`: input `{"internal_code": <code>}`; output
/// `{"external": <code>}`. A missing or non-string code maps like an unknown
/// code (`PROMOTION_REFUSED`); strict-parse failure is
/// `{"input_error": "INPUT_JSON_INVALID", "json_error": <code>}`.
pub fn external_refusal(input: &[u8]) -> String {
    let out = match json::parse(input) {
        Err(e) => input_json_invalid(e, "input_error"),
        Ok(v) => obj([(
            "external",
            s(refusal::external_code(
                v.get("internal_code").and_then(Value::as_str).unwrap_or(""),
            )),
        )]),
    };
    out.to_json_text()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    const THREE_JSON: &str =
        r#"{"matched_rule_ids":[],"reason_code":"REQUEST_JSON_INVALID","status":"invalid"}"#;
    const THREE_SCHEMA: &str =
        r#"{"matched_rule_ids":[],"reason_code":"REQUEST_SCHEMA_INVALID","status":"invalid"}"#;

    #[test]
    fn canonical_command() {
        assert_eq!(
            canonical(br#"{"b":1,"a":[true,null]}"#),
            format!(
                r#"{{"canonical":"{{\"a\":[true,null],\"b\":1}}","digest":"{}"}}"#,
                json::Digest::of_bytes(br#"{"a":[true,null],"b":1}"#)
            )
        );
        assert_eq!(canonical(b"[1.0]"), r#"{"error":"NUMBER_NOT_INTEGER"}"#);
        assert_eq!(
            canonical(b"{\"a\":1,\"a\":2}"),
            r#"{"error":"DUPLICATE_KEY"}"#
        );
    }

    #[test]
    fn decide_pre_decision_refusals_have_three_members() {
        assert_eq!(decide(b"{"), THREE_JSON);
        assert_eq!(decide(br#"{"state_revision":07}"#), THREE_JSON);
        for input in [
            r#"[]"#,
            r#"7"#,
            r#"{"bundle":null,"state_revision":1,"request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-02-30T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00+00:00","request":{}}"#,
            r#"{"bundle":null,"state_revision":-1,"now":"2026-01-01T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":"1","now":"2026-01-01T00:00:00Z","request":{}}"#,
            r#"{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00Z"}"#,
        ] {
            assert_eq!(decide(input.as_bytes()), THREE_SCHEMA, "{input}");
        }
    }

    #[test]
    fn decide_malformed_request_still_gets_a_decision() {
        let out = json::parse(decide(br#"{"bundle":null,"state_revision":9007199254740991,"now":"2024-02-29T23:59:59Z","request":{}}"#).as_bytes()).unwrap();
        assert_eq!(
            out.get("decided_at").and_then(Value::as_str),
            Some("2024-02-29T23:59:59Z")
        );
        assert_eq!(
            out.get("reason_code").and_then(Value::as_str),
            Some("REQUEST_SCHEMA_INVALID")
        );
        assert_eq!(
            out.get("request_id").and_then(Value::as_str),
            Some("unknown")
        );
        assert_eq!(out.as_obj().map(|m| m.len()), Some(13));
        let rejected = json::parse(
            decide(
                br#"{"bundle":"x","state_revision":0,"now":"2024-02-29T23:59:59Z","request":{}}"#,
            )
            .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            rejected.get("reason_code").and_then(Value::as_str),
            Some("REQUEST_SCHEMA_INVALID"),
            "schema is checked before policy"
        );
    }

    #[test]
    fn check_acceptance_and_external_refusal_commands() {
        assert_eq!(
            check_acceptance(b"{\"a\":NaN}"),
            r#"{"json_error":"INVALID_JSON","refusal":"INPUT_JSON_INVALID"}"#
        );
        assert_eq!(
            check_acceptance(b"{}"),
            r#"{"refusal":"CURRENT_STATE_MALFORMED"}"#
        );
        assert_eq!(
            check_acceptance(b"[]"),
            r#"{"refusal":"CURRENT_STATE_MALFORMED"}"#
        );
        assert_eq!(
            external_refusal(br#"{"internal_code":"DEADLINE_EXCEEDED"}"#),
            r#"{"external":"DEADLINE_EXCEEDED"}"#
        );
        assert_eq!(
            external_refusal(br#"{"internal_code":"STATE_REVISION_CONFLICT"}"#),
            r#"{"external":"PROMOTION_REFUSED"}"#
        );
        assert_eq!(
            external_refusal(br#"{"internal_code":7}"#),
            r#"{"external":"PROMOTION_REFUSED"}"#
        );
        assert_eq!(
            external_refusal(b"{"),
            r#"{"input_error":"INPUT_JSON_INVALID","json_error":"INVALID_JSON"}"#
        );
    }

    #[test]
    fn run_dispatches_only_kernel_commands() {
        for c in COMMANDS {
            assert!(run(c, b"{}").is_some(), "{c}");
        }
        for c in ["transition-scenario", "broker-scenario", "decide-batch", ""] {
            assert!(run(c, b"{}").is_none(), "{c}");
        }
    }
}
