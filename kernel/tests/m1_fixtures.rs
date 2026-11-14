//! M1 kernel fixture sets driven through the pure command layer.
//!
//! Mirrors the comparison rules of `m1/driver/parity.py`: each case carries
//! either `input` (a JSON value, passed to the command as its raw text) or
//! `input_text` (a JSON string whose content is the raw text the command must
//! parse itself); `compare` is `exact` (JSON equality of the whole output),
//! `fields` (equality of the listed members, a missing member compared as
//! missing) or `one_of` (any listed output). Expectations are read, never
//! written. Only the kernel sets (M2-02..M2-05) are run here; the broker and
//! promotion sets are M3 obligations (kernel/FIXTURE-MAP.md).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::{fixtures_dir, load_cases, Case};
use std::path::{Path, PathBuf};
use writ_kernel::commands;
use writ_kernel::json::{self, Value};

/// Kernel sets and their exact case counts (320 in total).
const KERNEL_SETS: [(&str, usize); 8] = [
    ("canonical-vector", 1),
    ("strict-json", 37),
    ("golden-decisions", 26),
    ("transition-decisions", 62),
    ("acceptance-binding", 97),
    ("external-refusal", 63),
    ("protocol-decide", 33),
    ("protocol-acceptance", 1),
];

/// M3 sets: present in the manifest, deliberately not run by the kernel.
const M3_SETS: [(&str, &str, usize); 4] = [
    ("broker-scenarios", "broker-scenario", 46),
    ("protocol-broker", "broker-scenario", 9),
    ("transition-scenarios", "transition-scenario", 66),
    ("protocol-transition", "transition-scenario", 6),
];

/// Driver comparison rules; returns a description of the first difference.
fn compare(case: &Case, output: &Value) -> Option<String> {
    let Value::Obj(out) = output else {
        return Some("output is not an object".into());
    };
    match case.compare.as_str() {
        "exact" => (output != &case.expect).then(|| "output differs from expect".into()),
        "one_of" => match case.expect.get("one_of") {
            Some(Value::Arr(alts)) => {
                (!alts.iter().any(|a| a == output)).then(|| "matches none of one_of".into())
            }
            _ => Some("one_of without a list".into()),
        },
        "fields" => {
            for f in &case.fields {
                let (got, want) = (out.get(f), case.expect.get(f));
                if got != want {
                    return Some(format!("{f}: got {got:?}, want {want:?}"));
                }
            }
            None
        }
        other => Some(format!("unknown compare mode {other}")),
    }
}

struct SetResult {
    cases: usize,
    passed: usize,
    failures: Vec<String>,
}

fn run_set(name: &str, command: &str) -> SetResult {
    let cases = load_cases(&fixtures_dir().join(format!("sets/{name}.jsonl")));
    let mut result = SetResult {
        cases: cases.len(),
        passed: 0,
        failures: Vec::new(),
    };
    for case in &cases {
        let first = commands::run(command, &case.input)
            .unwrap_or_else(|| panic!("unknown command {command}"));
        let second = commands::run(command, &case.input).unwrap();
        if first != second {
            result
                .failures
                .push(format!("{}: nondeterministic output", case.id));
            continue;
        }
        let output = match json::parse(first.as_bytes()) {
            Ok(v) => v,
            Err(e) => {
                result.failures.push(format!(
                    "{}: output is not strict JSON ({})",
                    case.id,
                    e.code()
                ));
                continue;
            }
        };
        match compare(case, &output) {
            None => result.passed += 1,
            Some(diff) => result
                .failures
                .push(format!("{}: {diff}; output {first}", case.id)),
        }
    }
    result
}

fn manifest_sets() -> Vec<(String, String)> {
    let text = std::fs::read(fixtures_dir().join("manifest.json")).unwrap();
    let manifest = json::parse(&text).unwrap();
    let Some(Value::Arr(sets)) = manifest.get("sets") else {
        panic!("manifest sets")
    };
    sets.iter()
        .map(|s| match (s.get("name"), s.get("command")) {
            (Some(Value::Str(n)), Some(Value::Str(c))) => (n.clone(), c.clone()),
            other => panic!("manifest entry {other:?}"),
        })
        .collect()
}

fn command_of(name: &str) -> String {
    manifest_sets()
        .into_iter()
        .find(|(n, _)| n == name)
        .map(|(_, c)| c)
        .unwrap()
}

fn assert_set(name: &str) {
    let expected = KERNEL_SETS
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, c)| *c)
        .unwrap();
    let r = run_set(name, &command_of(name));
    println!("{name}: {}/{} passed", r.passed, r.cases);
    assert_eq!(r.cases, expected, "{name}: case count");
    assert!(
        r.failures.is_empty(),
        "{name}: {} failures:\n{}",
        r.failures.len(),
        r.failures.join("\n")
    );
    assert_eq!(r.passed, expected);
}

#[test]
fn canonical_vector() {
    assert_set("canonical-vector");
}

#[test]
fn strict_json() {
    assert_set("strict-json");
}

#[test]
fn golden_decisions() {
    assert_set("golden-decisions");
}

#[test]
fn transition_decisions() {
    assert_set("transition-decisions");
}

#[test]
fn acceptance_binding() {
    assert_set("acceptance-binding");
}

#[test]
fn external_refusal() {
    assert_set("external-refusal");
}

#[test]
fn protocol_decide() {
    assert_set("protocol-decide");
}

#[test]
fn protocol_acceptance() {
    assert_set("protocol-acceptance");
}

/// The manifest holds exactly the 8 kernel sets plus the 4 M3 sets; the M3
/// sets keep their expected sizes and commands the kernel does not serve.
#[test]
fn manifest_partition_is_explicit() {
    let sets = manifest_sets();
    assert_eq!(sets.len(), KERNEL_SETS.len() + M3_SETS.len());
    let mut total = 0;
    for (name, count) in KERNEL_SETS {
        let command = command_of(name);
        assert!(
            commands::run(&command, b"{}").is_some(),
            "{name}: kernel serves {command}"
        );
        total += count;
    }
    assert_eq!(total, 320);
    for (name, command, count) in M3_SETS {
        assert_eq!(command_of(name), command);
        assert!(
            commands::run(command, b"{}").is_none(),
            "{command} is M3, not a kernel command"
        );
        assert_eq!(
            load_cases(&fixtures_dir().join(format!("sets/{name}.jsonl"))).len(),
            count
        );
    }
}

/// Bundle documents referenced by the fixtures load or are rejected exactly
/// as their fixture descriptions specify (D-017, D-019 N-4, D-020), and the
/// contract reference bundle is the one embedded in the golden set.
#[test]
fn contract_and_transition_bundles_load_or_reject_as_specified() {
    use writ_kernel::policy::{bundle_sha, load_bundle};
    let read = |p: PathBuf| json::parse(&std::fs::read(&p).unwrap()).unwrap();
    let reference = read(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/contracts/fixtures/policy/m0-reference.bundle.json"),
    );
    let loaded = load_bundle(&reference).unwrap();
    assert!(
        !loaded.supports_transitions(),
        "known capability only: not support (D-020)"
    );
    let golden = load_cases(&fixtures_dir().join("sets/golden-decisions.jsonl"));
    let embedded = json::parse(&golden[0].input).unwrap();
    assert_eq!(
        bundle_sha(embedded.get("bundle").unwrap()).unwrap(),
        loaded.sha
    );
    let dir = fixtures_dir().join("transition");
    for (file, loads) in [
        ("m1-transition.bundle.json", true),
        ("m1-transition-no-transition-support.bundle.json", true),
        (
            "m1-transition-transition-known-unsupported.bundle.json",
            true,
        ),
        ("m1-transition-unscoped.bundle.json", false),
        ("m1-transition-protection-missing.bundle.json", false),
        ("m1-transition-protection-duplicate.bundle.json", false),
        ("m1-transition-protection-self-edge.bundle.json", false),
        ("m1-transition-bypassable.bundle.json", false),
        ("m1-transition-protection-incomplete.bundle.json", false),
    ] {
        assert_eq!(load_bundle(&read(dir.join(file))).is_ok(), loads, "{file}");
    }
}
