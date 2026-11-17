use super::{
    claude_request, cli_request, encode, InputError, MAX_COMPONENT_BYTES, MAX_CONTROL_BYTES,
    MAX_TARGET_BYTES, MAX_TARGET_DEPTH,
};
use writ_kernel::json::{self, Value};

pub(super) enum Expected<'a> {
    Refused(InputError),
    Decision(&'a str),
}

pub(super) fn compare_cases(mut check: impl FnMut(&str, &[u8], &[u8], Expected<'_>)) {
    let depth_limit = vec!["a"; MAX_TARGET_DEPTH].join("/");
    let depth_over = format!("{depth_limit}/a");
    let component_limit = format!("{}a", "é".repeat(MAX_COMPONENT_BYTES / 2));
    let component_over = format!("{component_limit}a");
    let bytes_component = "a".repeat(MAX_TARGET_BYTES / 32 - 1);
    let bytes_prefix = [bytes_component.as_str(); 32].join("/");
    let bytes_limit = format!("{bytes_prefix}a");
    let bytes_over = format!("{bytes_limit}a");
    let malformed = || Expected::Refused(InputError::Malformed);
    for (id, path, expected) in [
        (
            "unicode-original",
            "docs/résumé.txt",
            Expected::Decision("docs/résumé.txt"),
        ),
        (
            "unicode-decomposed",
            "docs/re\u{301}sume\u{301}.txt",
            Expected::Decision("docs/re\u{301}sume\u{301}.txt"),
        ),
        ("max-depth", &depth_limit, Expected::Decision(&depth_limit)),
        (
            "max-component-bytes",
            &component_limit,
            Expected::Decision(&component_limit),
        ),
        (
            "max-target-bytes",
            &bytes_limit,
            Expected::Decision(&bytes_limit),
        ),
        ("excess-depth", &depth_over, malformed()),
        ("excess-component-bytes", &component_over, malformed()),
        ("excess-target-bytes", &bytes_over, malformed()),
        ("absolute-target", "/absolute", malformed()),
        ("parent-component", "../outside", malformed()),
        ("current-component", "docs/./report.txt", malformed()),
        ("empty-component", "docs//report.txt", malformed()),
        ("trailing-slash", "docs/", malformed()),
        ("backslash", "docs\\report.txt", malformed()),
        ("control-character", "docs/\u{85}report.txt", malformed()),
    ] {
        let hook = encode(&claude_request(path));
        let cli = encode(&cli_request(path));
        check(id, hook.as_bytes(), cli.as_bytes(), expected);
    }

    let path = "docs/report.txt";
    let hook = encode(&claude_request(path));
    let cli = encode(&cli_request(path));
    for (id, operation) in [
        ("unregistered-operation", "unregistered"),
        ("direct-promotion", "workflow.transition"),
    ] {
        check(
            id,
            hook.replace("writ_fs_create", operation).as_bytes(),
            cli.replace("create-file", operation).as_bytes(),
            Expected::Refused(InputError::UnknownOperation),
        );
    }
    let hook_duplicate = hook.replacen(
        "\"arguments\":{",
        r#""arguments":{"\u0066ile_path":"/fixture/tree/docs/report.txt","#,
        1,
    );
    let cli_duplicate = format!(r#"{{"\u0074arget":"{path}",{}"#, &cli[1..]);
    check(
        "duplicate-field",
        hook_duplicate.as_bytes(),
        cli_duplicate.as_bytes(),
        malformed(),
    );
    check(
        "unpaired-surrogate",
        hook.replace(path, r#"docs/\ud800.txt"#).as_bytes(),
        cli.replace(path, r#"docs/\ud800.txt"#).as_bytes(),
        malformed(),
    );
    check(
        "trailing-document",
        format!("{hook}{{}}").as_bytes(),
        format!("{cli}{{}}").as_bytes(),
        malformed(),
    );
    let invalid_utf8 = |request: &str| {
        let mut request_with_invalid_utf8 = request.as_bytes().to_vec();
        request_with_invalid_utf8[request.find(path).unwrap()] = 0xff;
        request_with_invalid_utf8
    };
    check(
        "invalid-utf8",
        &invalid_utf8(&hook),
        &invalid_utf8(&cli),
        malformed(),
    );

    let mut hook_limit = hook.as_bytes().to_vec();
    let mut cli_limit = cli.as_bytes().to_vec();
    hook_limit.resize(MAX_CONTROL_BYTES as usize, b' ');
    cli_limit.resize(MAX_CONTROL_BYTES as usize, b' ');
    check(
        "max-control-bytes",
        &hook_limit,
        &cli_limit,
        Expected::Decision(path),
    );
    hook_limit.push(b' ');
    cli_limit.push(b' ');
    check(
        "excess-control-bytes",
        &hook_limit,
        &cli_limit,
        Expected::Refused(InputError::Oversized),
    );
    let oversized_utf8 = "é".repeat(MAX_CONTROL_BYTES as usize / 2 + 1);
    check(
        "oversized-control",
        oversized_utf8.as_bytes(),
        oversized_utf8.as_bytes(),
        Expected::Refused(InputError::Oversized),
    );

    for (id, claim) in [
        (
            "claimed-principal",
            r#""principal":{"id":"claim","roles":["admin"]}"#,
        ),
        ("claimed-tenant", r#""tenant_id":"different-tenant""#),
        ("claimed-session", r#""session_id":"different-session""#),
        (
            "claimed-lineage",
            r#""parent_session_id":"parent","agent_id":"child""#,
        ),
        ("claimed-grant", r#""grant":"grant-1""#),
        ("claimed-approval", r#""approval":true"#),
    ] {
        let hook = hook.replacen("\"arguments\":{", &format!("\"arguments\":{{{claim},"), 1);
        let cli = format!("{{{claim},{}", &cli[1..]);
        check(id, hook.as_bytes(), cli.as_bytes(), malformed());
    }

    let Value::Arr(fixtures) = json::parse(include_bytes!("portability.json")).unwrap() else {
        panic!("fixture must be an array");
    };
    for case in fixtures {
        if let (Some(hook), Some(cli)) = (case.get("hook_input"), case.get("cli_input")) {
            let id = case.get("id").and_then(Value::as_str).unwrap();
            check(
                &format!("advisory-{id}"),
                encode(hook).as_bytes(),
                encode(cli).as_bytes(),
                malformed(),
            );
        }
    }
}
