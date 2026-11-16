use super::*;

pub(crate) fn limits() -> JobLimits {
    JobLimits {
        wall_time_ms: 10_000,
        memory_max_bytes: 256 << 20,
        pids_max: 64,
        cpu_max_quota_us: 50_000,
        cpu_max_period_us: 100_000,
        output_max_bytes: 1 << 20,
        input_max_bytes: 8 << 20,
        file_size_max_bytes: 16 << 20,
        nofile_max: 64,
        sigpending_max: 64,
    }
}

fn bounds() -> RequestBounds {
    RequestBounds {
        max_args: 4,
        max_arg_bytes: 32,
        max_env: 4,
        max_env_bytes: 32,
    }
}

fn request_text(edit: impl FnOnce(&mut Map)) -> Vec<u8> {
    let r = Request::new(
        "run-0a-1",
        "py312",
        "bin/check",
        vec!["--x".into()],
        vec![("LANG".into(), "C".into())],
        limits(),
    )
    .unwrap();
    let Value::Obj(mut m) = r.to_value() else {
        panic!("object")
    };
    edit(&mut m);
    json::canonical(&Value::Obj(m)).unwrap().into_bytes()
}

fn parse(bytes: &[u8]) -> Result<Request, LaunchCode> {
    parse_request(bytes, &bounds(), &limits(), &["py312"])
}

#[test]
fn valid_request_parses_with_digest_of_the_document() {
    let text = request_text(|_| {});
    let r = parse(&text).unwrap();
    assert_eq!(r.job_id, "run-0a-1");
    assert_eq!(r.program, "bin/check");
    assert_eq!(r.env, vec![("LANG".to_string(), "C".to_string())]);
    assert_eq!(
        r.digest,
        json::digest(&json::parse(&text).unwrap()).unwrap().as_str()
    );
    // The digest writ computes is the one the launcher computes.
    let mine = Request::new(
        "run-0a-1",
        "py312",
        "bin/check",
        vec!["--x".into()],
        vec![("LANG".into(), "C".into())],
        limits(),
    )
    .unwrap();
    assert_eq!(mine.digest, r.digest);
}

#[test]
fn unknown_or_missing_fields_are_invalid() {
    let extra = request_text(|m| {
        m.insert("source".into(), s("/etc"));
    });
    assert_eq!(parse(&extra), Err(LaunchCode::Invalid));
    let missing = request_text(|m| {
        m.remove("env");
    });
    assert_eq!(parse(&missing), Err(LaunchCode::Invalid));
    let contract = request_text(|m| {
        m.insert("contract".into(), s("writ-launch/2"));
    });
    assert_eq!(parse(&contract), Err(LaunchCode::Invalid));
    // Not strict JSON (duplicate key).
    assert_eq!(
        parse(br#"{"contract":"writ-launch/1","contract":"writ-launch/1"}"#),
        Err(LaunchCode::Invalid)
    );
}

#[test]
fn job_id_and_program_rules() {
    for bad in ["", "Run-1", "run_1", "run/1", "../x", &"a".repeat(65)] {
        let t = request_text(|m| {
            m.insert("job_id".into(), s(bad));
        });
        assert_eq!(parse(&t), Err(LaunchCode::Invalid), "job_id {bad:?}");
    }
    assert!(valid_job_id(&"a".repeat(64)));
    for bad in [
        "", "/bin/x", "a//b", "a/./b", "a/../b", "..", "a/", "a\u{1}b", "a\0b",
    ] {
        let t = request_text(|m| {
            m.insert("program".into(), s(bad));
        });
        assert_eq!(parse(&t), Err(LaunchCode::Invalid), "program {bad:?}");
    }
}

#[test]
fn args_and_env_are_bounded_and_checked() {
    let many = request_text(|m| {
        m.insert("args".into(), Value::Arr(vec![s("a"); 5]));
    });
    assert_eq!(parse(&many), Err(LaunchCode::Invalid));
    let long = request_text(|m| {
        m.insert("args".into(), Value::Arr(vec![s(&"a".repeat(33))]));
    });
    assert_eq!(parse(&long), Err(LaunchCode::Invalid));
    let nul = request_text(|m| {
        m.insert("args".into(), Value::Arr(vec![s("a\0b")]));
    });
    assert_eq!(parse(&nul), Err(LaunchCode::Invalid));
    for env in [
        vec![Value::Arr(vec![s("1A"), s("x")])],
        vec![Value::Arr(vec![s("A=B"), s("x")])],
        vec![
            Value::Arr(vec![s("A"), s("x")]),
            Value::Arr(vec![s("A"), s("y")]),
        ],
        vec![Value::Arr(vec![s("A")])],
        vec![Value::Arr(vec![s("A"), s("x\0")])],
    ] {
        let t = request_text(|m| {
            m.insert("env".into(), Value::Arr(env.clone()));
        });
        assert_eq!(parse(&t), Err(LaunchCode::Invalid), "env {env:?}");
    }
    assert!(valid_env_name("_A1") && !valid_env_name("") && !valid_env_name("a-b"));
}

#[test]
fn limits_are_required_positive_and_within_maxima() {
    let missing = request_text(|m| {
        let Some(Value::Obj(l)) = m.get_mut("limits") else {
            panic!()
        };
        l.remove("sigpending_max");
    });
    assert_eq!(parse(&missing), Err(LaunchCode::Invalid));
    let zero = request_text(|m| {
        let Some(Value::Obj(l)) = m.get_mut("limits") else {
            panic!()
        };
        l.insert("pids_max".into(), Value::Int(0));
    });
    assert_eq!(parse(&zero), Err(LaunchCode::Invalid));
    let period = request_text(|m| {
        let Some(Value::Obj(l)) = m.get_mut("limits") else {
            panic!()
        };
        l.insert("cpu_max_period_us".into(), Value::Int(999));
    });
    assert_eq!(parse(&period), Err(LaunchCode::Invalid));
    for name in LIMIT_NAMES {
        let above = request_text(|m| {
            let Some(Value::Obj(l)) = m.get_mut("limits") else {
                panic!()
            };
            let cur = l.get(name).unwrap().as_int().unwrap();
            l.insert(name.into(), Value::Int(cur + 1));
        });
        let got = parse(&above);
        // Raising the period above the maximum also leaves kernel range only at 1_000_000.
        assert_eq!(got, Err(LaunchCode::LimitExceeded), "{name}");
    }
}

#[test]
fn unknown_profile_is_refused_after_syntax() {
    let t = request_text(|m| {
        m.insert("runtime_profile".into(), s("node22"));
    });
    assert_eq!(parse(&t), Err(LaunchCode::ProfileUnknown));
}

#[test]
fn hello_round_trip_and_matching_ignores_instance() {
    let h = Hello {
        launcher_digest: "sha256:aa".into(),
        exec_digest: "sha256:bb".into(),
        bwrap_digest: "sha256:cc".into(),
        bwrap_profile_digest: "sha256:dd".into(),
        apparmor_profile_digest: "sha256:ee".into(),
        runtime_profiles: vec![("py312".into(), "sha256:ff".into())],
        maxima_digest: "sha256:11".into(),
        instance: "0-1-2".into(),
    };
    let text = json::canonical(&h.to_value()).unwrap();
    let back = Hello::parse(text.as_bytes()).unwrap();
    assert_eq!(back, h);
    let other_instance = Hello {
        instance: "9".into(),
        ..h.clone()
    };
    assert!(h.matches(&other_instance));
    let other_bwrap = Hello {
        bwrap_digest: "sha256:00".into(),
        ..h.clone()
    };
    assert!(!h.matches(&other_bwrap));
    assert_eq!(h.runtime_profile("py312"), Some("sha256:ff"));
    let mut extra = h.to_value();
    if let Value::Obj(m) = &mut extra {
        m.insert("x".into(), s("y"));
    }
    assert!(Hello::parse(json::canonical(&extra).unwrap().as_bytes()).is_none());
}

#[test]
fn result_round_trip_and_frames() {
    let f = Finished {
        job_id: "run-1".into(),
        request_digest: "sha256:ab".into(),
        reason: Reason::Exited,
        exit_status: Some(0),
        signal: None,
        kill_used: true,
        populated_zero: true,
        memory_events: [0, 0, 0],
        pids_max_events: 0,
        cpu_usage_usec: 1234,
        output_bytes: [5, 0],
        digests: ResultDigests {
            bwrap: "sha256:1".into(),
            bwrap_profile: "sha256:2".into(),
            apparmor_profile: "sha256:3".into(),
            runtime_profile: "sha256:4".into(),
            exec: "sha256:5".into(),
        },
    };
    let text = json::canonical(&f.to_value()).unwrap();
    assert_eq!(Finished::parse(text.as_bytes()), Some(f.clone()));
    assert_eq!(
        parse_launcher_frame(text.as_bytes()),
        Some(LauncherFrame::Finished(Box::new(f)))
    );
    let r = json::canonical(&refused("run-1", LaunchCode::Busy)).unwrap();
    assert_eq!(
        parse_launcher_frame(r.as_bytes()),
        Some(LauncherFrame::Refused("run-1".into(), LaunchCode::Busy))
    );
    let st = json::canonical(&started("run-1")).unwrap();
    assert_eq!(
        parse_launcher_frame(st.as_bytes()),
        Some(LauncherFrame::Started("run-1".into()))
    );
    let c = json::canonical(&cancel("run-1")).unwrap();
    assert!(is_cancel(c.as_bytes(), "run-1"));
    assert!(!is_cancel(c.as_bytes(), "run-2"));
    assert!(!is_cancel(st.as_bytes(), "run-1"));
    for code in ["LAUNCH_INVALID", "LAUNCH_UNAVAILABLE"] {
        assert_eq!(LaunchCode::parse(code).unwrap().as_str(), code);
    }
}
