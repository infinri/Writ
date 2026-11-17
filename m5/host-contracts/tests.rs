use super::*;
use writ_kernel::json::{obj, s};

const ROOT: &str = "/fixture/tree";
const SNAPSHOT: &str = "snap-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const MANIFEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CONTENT: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn encode(value: &Value) -> String {
    json::canonical(value).unwrap()
}

fn claude_request(path: &str) -> Value {
    obj([
        ("name", s("writ_fs_create")),
        (
            "arguments",
            obj([
                ("file_path", s(&format!("{ROOT}/{path}"))),
                ("source_path", s("source.txt")),
                ("snapshot_id", s(SNAPSHOT)),
                ("manifest_digest", s(MANIFEST)),
                ("content_digest", s(CONTENT)),
            ]),
        ),
    ])
}

fn cli_request(path: &str) -> Value {
    obj([
        ("command", s("create-file")),
        ("target", s(path)),
        ("source", s("source.txt")),
        ("snapshot", s(SNAPSHOT)),
        ("manifest", s(MANIFEST)),
        ("content", s(CONTENT)),
    ])
}

#[test]
fn both_advisory_paths_project_paired_fixture_inputs() {
    let file = json::parse(include_bytes!("portability.json")).unwrap();
    let Value::Arr(cases) = file else {
        panic!("fixture must be an array")
    };
    let mut count = 0;
    for case in cases.iter().map(|case| case.as_obj().unwrap()) {
        let Some(expected) = case.get("expected_observation").and_then(Value::as_obj) else {
            continue;
        };
        let hook = encode(&case["hook_input"]);
        let cli = encode(&case["cli_input"]);
        let phase = match text(expected, "event").unwrap() {
            "before_tool" => Phase::BeforeTool,
            "permission_prompt" => Phase::PermissionPrompt,
            event => panic!("unsupported fixture event {event}"),
        };
        let expected = WriteObservation {
            phase,
            relative_path: text(expected, "relative_path").unwrap().to_owned(),
        };
        assert_eq!(
            claude_observation(hook.as_bytes(), ROOT),
            Ok(expected.clone())
        );
        assert_eq!(cli_observation(cli.as_bytes()), Ok(expected));
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
        count += 1;
    }
    assert_eq!(count, 4);
}

#[test]
fn create_proposals_are_equivalent_and_preserve_exact_path_bytes() {
    for path in [
        "docs/résumé.txt",
        "docs/re\u{301}sume\u{301}.txt",
        "link/result.txt",
    ] {
        let hook = encode(&claude_request(path));
        let cli = encode(&cli_request(path));
        let expected = CreateProposal {
            relative_path: path.to_string(),
            source_path: "source.txt".to_string(),
            snapshot_id: SNAPSHOT.to_string(),
            manifest_digest: MANIFEST.to_string(),
            content_digest: CONTENT.to_string(),
        };
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Ok(expected.clone())
        );
        assert_eq!(cli_protected_write(cli.as_bytes()), Ok(expected));
    }
}

#[test]
fn strict_parsing_refuses_ambiguous_json_on_both_modes() {
    for invalid in [
        br#"{"x": {"path": "one", "\u0070ath": "two"}}"#.as_slice(),
        br#"{"x": "\ud800"}"#.as_slice(),
        br#"{"x": 1e0}"#.as_slice(),
        br#"{}{}"#.as_slice(),
        b"{\"x\":\"\xff\"}".as_slice(),
    ] {
        assert_eq!(
            claude_observation(invalid, ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(cli_observation(invalid), Err(InputError::Malformed));
        assert_eq!(
            claude_protected_write(invalid, ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(cli_protected_write(invalid), Err(InputError::Malformed));
    }
}

#[test]
fn byte_limit_is_enforced_before_parsing() {
    let wire = "é".repeat(MAX_CONTROL_BYTES as usize / 2 + 1);
    assert_eq!(
        claude_observation(wire.as_bytes(), ROOT),
        Err(InputError::Oversized)
    );
    assert_eq!(cli_observation(wire.as_bytes()), Err(InputError::Oversized));
    for (request, action) in [
        (
            encode(&claude_request("report.txt")),
            (|bytes: &[u8]| claude_protected_write(bytes, ROOT))
                as fn(&[u8]) -> Result<CreateProposal, InputError>,
        ),
        (encode(&cli_request("report.txt")), cli_protected_write),
    ] {
        let mut boundary = request.into_bytes();
        boundary.resize(MAX_CONTROL_BYTES as usize, b' ');
        assert!(action(&boundary).is_ok());
        boundary.push(b' ');
        assert_eq!(action(&boundary), Err(InputError::Oversized));
    }
}

#[test]
fn protected_intake_refuses_paths_outside_the_lexical_contract() {
    for path in [
        "",
        "/absolute",
        "../escape",
        "nested/./file",
        "nested//file",
        "end/",
        "x\\y",
        "x\0y",
        "x\u{85}y",
    ] {
        let hook = encode(&claude_request(path));
        let cli = encode(&cli_request(path));
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
    }
    let hook = encode(&claude_request("report.txt")).replace(ROOT, "/fixture/tree-other");
    assert_eq!(
        claude_protected_write(hook.as_bytes(), ROOT),
        Err(InputError::Malformed)
    );
}

#[test]
fn unknown_protected_operations_are_refused_and_cannot_become_promotions() {
    let hook =
        encode(&claude_request("report.txt")).replace("writ_fs_create", "workflow.transition");
    let cli = encode(&cli_request("report.txt")).replace("create-file", "workflow.transition");
    assert_eq!(
        claude_protected_write(hook.as_bytes(), ROOT),
        Err(InputError::UnknownOperation)
    );
    assert_eq!(
        cli_protected_write(cli.as_bytes()),
        Err(InputError::UnknownOperation)
    );
}

#[test]
fn protected_intake_rejects_choice_of_authority_or_untrusted_file_content() {
    let hook = encode(&claude_request("report.txt"));
    let cli = encode(&cli_request("report.txt"));
    for claims in [
        r#""grant": "grant-1""#,
        r#""session_id": "claimed-session""#,
        r#""approval": true"#,
        r#""tenant_id": "another-tenant""#,
        r#""content_bytes": "untrusted file body""#,
        r#""mode": "replace_if""#,
    ] {
        let hook = hook.replacen("\"arguments\":{", &format!("\"arguments\":{{{claims},"), 1);
        let cli = format!("{{{claims},{}", &cli[1..]);
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
    }
    let invalid_digest = "sha256:1";
    let hook = hook.replace(CONTENT, invalid_digest);
    let cli = cli.replace(CONTENT, invalid_digest);
    assert_eq!(
        claude_protected_write(hook.as_bytes(), ROOT),
        Err(InputError::Malformed)
    );
    assert_eq!(
        cli_protected_write(cli.as_bytes()),
        Err(InputError::Malformed)
    );
}

#[test]
fn embedded_advisory_claims_do_not_change_projection() {
    let hook = br#"{
        "hook_event_name":"PermissionRequest", "tool_name":"Write",
        "tool_input":{"file_path":"/fixture/tree/report.txt", "content":"discarded",
                      "grant":"g1", "action":{"kind":"workflow.transition"}},
        "session_id":"agent-claims-id", "approval":true
    }"#;
    let cli = br#"{
        "phase":"prompt", "operation":"file-write", "target":"report.txt",
        "diagnostics":{"grant":"g1", "action":{"kind":"workflow.transition"}},
        "approval":true, "session":"agent-claims-id"
    }"#;
    let expected = WriteObservation {
        phase: Phase::PermissionPrompt,
        relative_path: "report.txt".to_string(),
    };
    assert_eq!(claude_observation(hook, ROOT), Ok(expected.clone()));
    assert_eq!(cli_observation(cli), Ok(expected));
}

#[test]
fn storage_id_and_source_syntax_are_distinct_from_target_syntax() {
    let hook = encode(&claude_request("résumé.txt"));
    let cli = encode(&cli_request("résumé.txt"));
    assert!(claude_protected_write(hook.as_bytes(), ROOT).is_ok());
    assert!(cli_protected_write(cli.as_bytes()).is_ok());
    for invalid_id in ["snap-1", "snap-cccc", CONTENT] {
        let hook = hook.replace(SNAPSHOT, invalid_id);
        let cli = cli.replace(SNAPSHOT, invalid_id);
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
    }
    for bad_source in ["réport.txt", "source//doc", "../doc"] {
        let hook = hook.replace("source.txt", bad_source);
        let cli = cli.replace("source.txt", bad_source);
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
    }
}

#[test]
fn target_depth_length_and_component_bounds_agree() {
    let too_deep = format!("a{}", "/a".repeat(MAX_TARGET_DEPTH));
    let too_wide = "é".repeat(MAX_COMPONENT_BYTES / 2 + 1);
    let component = "a".repeat(MAX_COMPONENT_BYTES);
    let too_long = [component.as_str(); 17].join("/");
    for path in [too_deep, too_wide, too_long] {
        let hook = encode(&claude_request(&path));
        let cli = encode(&cli_request(&path));
        assert_eq!(
            claude_protected_write(hook.as_bytes(), ROOT),
            Err(InputError::Malformed)
        );
        assert_eq!(
            cli_protected_write(cli.as_bytes()),
            Err(InputError::Malformed)
        );
    }
    let hook = encode(&claude_request("report.txt"));
    for root in [
        "",
        "/",
        "fixture/tree",
        "/fixture/tree/",
        "/fixture/../tree",
    ] {
        assert_eq!(
            claude_protected_write(hook.as_bytes(), root),
            Err(InputError::Malformed)
        );
    }
}

#[path = "kernel_boundary.rs"]
mod kernel_boundary;

#[path = "kernel_cases.rs"]
mod kernel_cases;
