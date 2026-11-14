//! Trusted-constructor allow-list (M4-01c; M4-INTERFACES R3).
//!
//! Rust cannot restrict callers of a public function across crates, so the
//! capability constructors of `writ_broker::human` are confined by this scan
//! of every workspace `.rs` file (skipping `target/`, `.claude/`, `.git/`,
//! `.venv/` and `node_modules/` directories):
//!
//! - `VerifierCapability::for_verifier_implementation`: production code only
//!   under `approval/` (the `writ-approval` crate), and the one non-shipping
//!   test verifier of the closure adapter
//!   (`persistence/sqlite/examples/m3_line_adapter/signoff.rs`, a Cargo example);
//! - `LedgerCapability::for_ledger_implementation`: production code only
//!   under `persistence/sqlite/src/` (the SQLite ledger);
//! - either, in test code of the `broker/`, `approval/` or
//!   `persistence/sqlite/` crates.
//!
//! Test code is detected explicitly: a path with a `tests` directory
//! component, a file named `*_tests.rs` or `test_support.rs`, or the lines of
//! a file from a `#[cfg(test)]` attribute followed (after other attribute
//! lines only) by `mod tests {` to the end of the file. Clippy's
//! `items_after_test_module` lint (denied by `-D warnings`) keeps that module
//! the last item. A word containing `test` (for example `attestation`) does
//! not count. Comment lines and the `fn` definitions themselves are ignored.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

const VERIFIER: &str = "for_verifier_implementation";
const LEDGER: &str = "for_ledger_implementation";

/// Production path prefixes allowed per capability constructor.
const PRODUCTION_ALLOWED: [(&str, &[&str]); 2] = [
    // The closure adapter's non-shipping test verifier (a Cargo example, never in a release
    // binary; M4-11 signoffs) is named exactly; no other example file is allowed.
    (
        VERIFIER,
        &[
            "approval/",
            "persistence/sqlite/examples/m3_line_adapter/signoff.rs",
        ],
    ),
    (LEDGER, &["persistence/sqlite/src/"]),
];

/// Crates whose test code may use either capability.
const TEST_ALLOWED: &[&str] = &["broker/", "approval/", "persistence/sqlite/"];

const SKIPPED_DIRS: &[&str] = &["target", ".claude", ".git", ".venv", "node_modules"];

fn is_test_file(rel: &str) -> bool {
    let mut parts = rel.split('/').collect::<Vec<_>>();
    let name = parts.pop().unwrap_or_default();
    parts.contains(&"tests") || name.ends_with("_tests.rs") || name == "test_support.rs"
}

/// The first line index of a trailing `#[cfg(test)] mod tests {` region.
fn test_module_start(lines: &[&str]) -> Option<usize> {
    for (i, line) in lines.iter().enumerate() {
        if line.trim() != "#[cfg(test)]" {
            continue;
        }
        let next = lines[i + 1..]
            .iter()
            .map(|l| l.trim())
            .find(|l| !(l.starts_with("#[") && l.ends_with(']')));
        if next == Some("mod tests {") {
            return Some(i);
        }
    }
    None
}

/// Every disallowed capability-constructor call in `text` at workspace
/// path `rel` (forward slashes), as `path:line: name`.
fn violations(rel: &str, text: &str) -> Vec<String> {
    let lines: Vec<&str> = text.lines().collect();
    let test_from = if is_test_file(rel) {
        Some(0)
    } else {
        test_module_start(&lines)
    };
    let in_test_crate = TEST_ALLOWED.iter().any(|p| rel.starts_with(p));
    let mut found = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let code = line.trim_start();
        if code.starts_with("//") {
            continue;
        }
        for (name, prefixes) in PRODUCTION_ALLOWED {
            let calls = code.matches(name).count() - code.matches(&format!("fn {name}")).count();
            if calls == 0 {
                continue;
            }
            let test_code = test_from.is_some_and(|start| i >= start);
            let allowed =
                prefixes.iter().any(|p| rel.starts_with(p)) || (test_code && in_test_crate);
            if !allowed {
                found.push(format!("{rel}:{}: {name}", i + 1));
            }
        }
    }
    found
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if path.is_dir() {
            if !SKIPPED_DIRS.contains(&name) {
                walk(&path, out)?;
            }
        } else if name.ends_with(".rs") {
            out.push(path);
        }
    }
    Ok(())
}

#[test]
fn capability_constructors_are_called_only_from_the_allow_list() -> io::Result<()> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| io::Error::other("no workspace root"))?;
    let mut files = Vec::new();
    walk(root, &mut files)?;
    let mut found = Vec::new();
    let mut saw_definitions = false;
    for path in &files {
        let rel = path
            .strip_prefix(root)
            .map_err(io::Error::other)?
            .to_string_lossy()
            .replace('\\', "/");
        let text = fs::read_to_string(path)?;
        if rel == "broker/src/human.rs" {
            saw_definitions = [VERIFIER, LEDGER]
                .iter()
                .all(|n| text.contains(&format!("pub fn {n}()")));
        }
        found.extend(violations(&rel, &text));
    }
    assert!(saw_definitions, "the scan did not reach the definitions");
    assert!(
        found.is_empty(),
        "capability constructors outside the allow-list:\n{}",
        found.join("\n")
    );
    Ok(())
}

#[test]
fn the_scanner_flags_synthetic_violations() {
    let call_v = "let c = VerifierCapability::for_verifier_implementation();";
    let call_l = "let c = writ_broker::human::LedgerCapability::for_ledger_implementation();";
    let both = format!("fn f() {{\n    {call_v}\n    {call_l}\n}}\n");
    // Outside every allowed crate: both refused, test code or not.
    assert_eq!(violations("kernel/src/lib.rs", &both).len(), 2);
    assert_eq!(violations("kernel/tests/x.rs", &both).len(), 2);
    assert_eq!(violations("m1/rust/src/main.rs", &both).len(), 2);
    // Broker production code (including a name merely containing "test").
    assert_eq!(violations("broker/src/execute.rs", &both).len(), 2);
    assert_eq!(violations("broker/src/attestation.rs", &both).len(), 2);
    // Each crate only its own capability in production code.
    assert_eq!(
        violations("approval/src/verifier.rs", &both),
        ["approval/src/verifier.rs:3: for_ledger_implementation"]
    );
    assert_eq!(
        violations("persistence/sqlite/src/approval_store.rs", &both),
        ["persistence/sqlite/src/approval_store.rs:2: for_verifier_implementation"]
    );
    // An aliased type is still caught by the method name.
    let aliased = "use writ_broker::human::VerifierCapability as V;\nlet c = V::for_verifier_implementation();\n";
    assert_eq!(violations("broker/src/policy.rs", aliased).len(), 1);
    // Test code of an allowed crate may use either.
    assert!(violations("broker/tests/x.rs", &both).is_empty());
    assert!(violations("persistence/sqlite/src/grant_tests.rs", &both).is_empty());
    assert!(violations("broker/src/test_support.rs", &both).is_empty());
    let module = format!(
        "pub fn real() {{}}\n\n#[cfg(test)]\n#[allow(clippy::unwrap_used)]\nmod tests {{\n{both}}}\n"
    );
    assert!(violations("broker/src/approval.rs", &module).is_empty());
    // A call before the test module, or under a non-module cfg(test), is refused.
    let before = format!("{call_v}\n{module}");
    assert_eq!(violations("broker/src/approval.rs", &before).len(), 1);
    let item = format!("#[cfg(test)]\nfn helper() {{}}\n{call_v}\n");
    assert_eq!(violations("broker/src/approval.rs", &item).len(), 1);
    let other_mod = format!("#[cfg(test)]\nmod fixtures {{\n{call_l}\n}}\n");
    assert_eq!(violations("broker/src/approval.rs", &other_mod).len(), 1);
    // Comments and the definitions are not calls.
    let defs = "/// See for_verifier_implementation.\npub fn for_verifier_implementation() -> X {}\npub fn for_ledger_implementation() -> Y {}\n";
    assert!(violations("broker/src/human.rs", defs).is_empty());
}
