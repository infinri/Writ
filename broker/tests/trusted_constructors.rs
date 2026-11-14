//! Trusted-constructor allow-list (M4-01c; M4-INTERFACES R3, C3).
//!
//! Rust cannot restrict callers of a public function across crates, so the
//! capability constructors of `writ_broker::human` are confined by this scan
//! of every workspace `.rs` file (skipping `target/`, `.claude/`, `.git/`,
//! `.venv/` and `node_modules/` directories):
//!
//! - `VerifierCapability::for_verifier_implementation`: production code only
//!   under `approval/` (the `writ-approval` crate);
//! - `LedgerCapability::for_ledger_implementation`: production code only
//!   under `persistence/sqlite/src/` (the SQLite ledger);
//! - either, in test code of the `broker/`, `approval/` or
//!   `persistence/sqlite/` crates.
//!
//! C3 also flags, in production code under `approval/` and
//! `persistence/sqlite/src/`, any `pub` (or `pub(..)`) function, `const` or
//! `static` whose type or return type names a `*Capability` or a
//! `VerifiedAssertion`: those crates may use a capability, never hand one (or
//! an assertion built with one) to other code.
//!
//! Test code is detected explicitly, never by a file name (C3): a path with a
//! `tests` directory component (a Cargo integration-test crate); a path with
//! an `examples` directory component (a non-shipping Cargo example, never part
//! of a library); a source file whose declaring `mod <name>;` line (in the
//! parent `lib.rs`, `main.rs`, `mod.rs` or `<parent>.rs`) carries
//! `#[cfg(test)]` among the attribute lines directly above it, or whose
//! declaring file is itself test code; or the lines of a file from a
//! `#[cfg(test)]` attribute followed (after other attribute lines only) by
//! `mod tests {` to the end of the file. Clippy's `items_after_test_module`
//! lint (denied by `-D warnings`) keeps that module the last item. A word
//! containing `test` (for example `attestation`) does not count. Comment lines
//! and the `fn` definitions themselves are ignored.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

const VERIFIER: &str = "for_verifier_implementation";
const LEDGER: &str = "for_ledger_implementation";

/// Production path prefixes allowed per capability constructor.
const PRODUCTION_ALLOWED: [(&str, &[&str]); 2] = [
    (VERIFIER, &["approval/"]),
    (LEDGER, &["persistence/sqlite/src/"]),
];

/// Crates whose test code may use either capability.
const TEST_ALLOWED: &[&str] = &["broker/", "approval/", "persistence/sqlite/"];

/// Production code that must never return a capability or an assertion (C3).
const NO_TRUST_EXPORT: &[&str] = &["approval/", "persistence/sqlite/src/"];

const SKIPPED_DIRS: &[&str] = &["target", ".claude", ".git", ".venv", "node_modules"];

/// A directory component that makes a path test code by Cargo's layout.
fn is_test_layout(rel: &str) -> bool {
    let mut parts = rel.split('/').collect::<Vec<_>>();
    parts.pop();
    parts.contains(&"tests") || parts.contains(&"examples")
}

/// Candidate files that declare the module of `rel`, with that module's name.
fn declarers(rel: &str) -> Option<(Vec<String>, String)> {
    let (dir, file) = rel.rsplit_once('/')?;
    let stem = file.strip_suffix(".rs")?;
    if matches!(stem, "lib" | "main") {
        return None;
    }
    let (dir, name) = if stem == "mod" {
        let (parent, name) = dir.rsplit_once('/')?;
        (parent.to_string(), name.to_string())
    } else {
        (dir.to_string(), stem.to_string())
    };
    let mut candidates = vec![
        format!("{dir}/lib.rs"),
        format!("{dir}/main.rs"),
        format!("{dir}/mod.rs"),
    ];
    if let Some((parent, last)) = dir.rsplit_once('/') {
        candidates.push(format!("{parent}/{last}.rs"));
    }
    Some((candidates, name))
}

/// `Some(true)` when `text` declares `mod name;` under a `#[cfg(test)]`
/// attribute, `Some(false)` when it declares it without, `None` when it does
/// not declare it.
fn declares(text: &str, name: &str) -> Option<bool> {
    let lines: Vec<&str> = text.lines().map(str::trim).collect();
    for (i, line) in lines.iter().enumerate() {
        let decl = line
            .strip_prefix("pub(crate) ")
            .or_else(|| line.strip_prefix("pub(super) "))
            .or_else(|| line.strip_prefix("pub "))
            .unwrap_or(line);
        if decl != format!("mod {name};") {
            continue;
        }
        let cfg_test = lines[..i]
            .iter()
            .rev()
            .take_while(|l| l.starts_with("#[") && l.ends_with(']'))
            .any(|l| *l == "#[cfg(test)]");
        return Some(cfg_test);
    }
    None
}

/// Whether the file at `rel` is test code (module documentation), reading
/// other workspace files through `read`.
fn is_test_file(rel: &str, read: &dyn Fn(&str) -> Option<String>) -> bool {
    if is_test_layout(rel) {
        return true;
    }
    let Some((candidates, name)) = declarers(rel) else {
        return false;
    };
    for candidate in candidates {
        let Some(text) = read(&candidate) else {
            continue;
        };
        match declares(&text, &name) {
            Some(true) => return true,
            Some(false) => return is_test_file(&candidate, read),
            None => {}
        }
    }
    false
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

/// Whether `ty` (a type or return type) names a capability or an assertion.
fn trusted_type(ty: &str) -> bool {
    ty.split(|c: char| !(c.is_alphanumeric() || c == '_'))
        .any(|w| (w.ends_with("Capability") && w != "Capability") || w == "VerifiedAssertion")
}

/// The `pub` items of `lines` (from index `from` on, outside test code) whose
/// type or return type names a capability or an assertion: line numbers.
fn trusted_exports(lines: &[&str], test_from: Option<usize>) -> Vec<usize> {
    let mut found = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if test_from.is_some_and(|start| i >= start) {
            break;
        }
        let code = line.trim_start();
        if !code.starts_with("pub") || code.starts_with("//") {
            continue;
        }
        let item = code
            .split_once(' ')
            .map(|(vis, rest)| (vis, rest.trim_start()));
        let Some((vis, rest)) = item else { continue };
        if !(vis == "pub" || vis.starts_with("pub(")) {
            continue;
        }
        let rest = rest
            .trim_start_matches("const fn ")
            .trim_start_matches("unsafe fn ");
        if let Some(fn_rest) = rest.strip_prefix("fn ").or_else(|| {
            code.contains(" fn ")
                .then(|| code.split_once(" fn ").map(|(_, r)| r))
                .flatten()
        }) {
            // The signature up to its body or end, possibly over several lines.
            let mut sig = fn_rest.to_string();
            let mut j = i;
            while !sig.contains('{') && !sig.contains(';') && j + 1 < lines.len() {
                j += 1;
                sig.push(' ');
                sig.push_str(lines[j].trim());
            }
            let sig = sig.split(['{', ';']).next().unwrap_or_default();
            if let Some((_, ret)) = sig.rsplit_once("->") {
                if trusted_type(ret) {
                    found.push(i + 1);
                }
            }
        } else if let Some(decl) = rest
            .strip_prefix("const ")
            .or_else(|| rest.strip_prefix("static "))
        {
            if decl
                .split_once(':')
                .is_some_and(|(_, ty)| trusted_type(ty.split('=').next().unwrap_or_default()))
            {
                found.push(i + 1);
            }
        }
    }
    found
}

/// Every disallowed capability-constructor call, and every C3 trusted export,
/// in `text` at workspace path `rel` (forward slashes), as `path:line: name`.
/// `test_file` says whether the whole file is test code.
fn violations(rel: &str, text: &str, test_file: bool) -> Vec<String> {
    let lines: Vec<&str> = text.lines().collect();
    let test_from = if test_file {
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
    if NO_TRUST_EXPORT.iter().any(|p| rel.starts_with(p)) {
        for line in trusted_exports(&lines, test_from) {
            found.push(format!("{rel}:{line}: trusted export"));
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
    let mut texts = BTreeMap::new();
    for path in &files {
        let rel = path
            .strip_prefix(root)
            .map_err(io::Error::other)?
            .to_string_lossy()
            .replace('\\', "/");
        texts.insert(rel, fs::read_to_string(path)?);
    }
    let read = |rel: &str| texts.get(rel).cloned();
    let mut found = Vec::new();
    let mut saw_definitions = false;
    for (rel, text) in &texts {
        if rel == "broker/src/human.rs" {
            saw_definitions = [VERIFIER, LEDGER]
                .iter()
                .all(|n| text.contains(&format!("pub fn {n}()")));
        }
        found.extend(violations(rel, text, is_test_file(rel, &read)));
    }
    assert!(saw_definitions, "the scan did not reach the definitions");
    // The declared test modules of the SQLite crate are recognized by their `mod` line.
    assert!(is_test_file("persistence/sqlite/src/m4_support.rs", &read));
    assert!(is_test_file(
        "persistence/sqlite/src/transition_props.rs",
        &read
    ));
    assert!(!is_test_file(
        "persistence/sqlite/src/approval_store.rs",
        &read
    ));
    assert!(
        found.is_empty(),
        "capability constructors or trusted exports outside the allow-list:\n{}",
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
    assert_eq!(violations("kernel/src/lib.rs", &both, false).len(), 2);
    assert_eq!(violations("kernel/tests/x.rs", &both, true).len(), 2);
    assert_eq!(violations("m1/rust/src/main.rs", &both, false).len(), 2);
    // Broker production code (including a name merely containing "test").
    assert_eq!(violations("broker/src/execute.rs", &both, false).len(), 2);
    assert_eq!(
        violations("broker/src/attestation.rs", &both, false).len(),
        2
    );
    // Each crate only its own capability in production code.
    assert_eq!(
        violations("approval/src/verifier.rs", &both, false),
        ["approval/src/verifier.rs:3: for_ledger_implementation"]
    );
    assert_eq!(
        violations("persistence/sqlite/src/approval_store.rs", &both, false),
        ["persistence/sqlite/src/approval_store.rs:2: for_verifier_implementation"]
    );
    // An aliased type is still caught by the method name.
    let aliased = "use writ_broker::human::VerifierCapability as V;\nlet c = V::for_verifier_implementation();\n";
    assert_eq!(violations("broker/src/policy.rs", aliased, false).len(), 1);
    // Test code of an allowed crate may use either.
    assert!(violations("broker/tests/x.rs", &both, true).is_empty());
    assert!(violations("persistence/sqlite/src/grant_tests.rs", &both, true).is_empty());
    let module = format!(
        "pub fn real() {{}}\n\n#[cfg(test)]\n#[allow(clippy::unwrap_used)]\nmod tests {{\n{both}}}\n"
    );
    assert!(violations("broker/src/approval.rs", &module, false).is_empty());
    // A call before the test module, or under a non-module cfg(test), is refused.
    let before = format!("{call_v}\n{module}");
    assert_eq!(
        violations("broker/src/approval.rs", &before, false).len(),
        1
    );
    let item = format!("#[cfg(test)]\nfn helper() {{}}\n{call_v}\n");
    assert_eq!(violations("broker/src/approval.rs", &item, false).len(), 1);
    let other_mod = format!("#[cfg(test)]\nmod fixtures {{\n{call_l}\n}}\n");
    assert_eq!(
        violations("broker/src/approval.rs", &other_mod, false).len(),
        1
    );
    // Comments and the definitions are not calls.
    let defs = "/// See for_verifier_implementation.\npub fn for_verifier_implementation() -> X {}\npub fn for_ledger_implementation() -> Y {}\n";
    assert!(violations("broker/src/human.rs", defs, false).is_empty());
}

#[test]
fn test_code_is_recognized_by_its_declaring_mod_line_never_by_its_name() {
    let files: BTreeMap<&str, &str> = [
        (
            "persistence/sqlite/src/lib.rs",
            "mod approval_store;\n#[cfg(test)]\nmod grant_tests;\n\n#[cfg(test)]\n#[allow(clippy::unwrap_used)]\nmod support;\nmod sneaky_tests;\npub(crate) mod nested;\n",
        ),
        ("persistence/sqlite/src/nested.rs", "#[cfg(test)]\nmod inner;\nmod plain;\n"),
        ("persistence/sqlite/src/support.rs", "mod deeper;\n"),
    ]
    .into_iter()
    .collect();
    let read = |rel: &str| files.get(rel).map(|t| t.to_string());
    let test = |rel: &str| is_test_file(rel, &read);
    assert!(test("persistence/sqlite/src/grant_tests.rs"));
    assert!(
        test("persistence/sqlite/src/support.rs"),
        "other attributes between"
    );
    assert!(
        test("persistence/sqlite/src/support/deeper.rs"),
        "inside a test module"
    );
    assert!(test("persistence/sqlite/src/nested/inner.rs"));
    assert!(!test("persistence/sqlite/src/nested/plain.rs"));
    assert!(
        !test("persistence/sqlite/src/sneaky_tests.rs"),
        "a name is not a test"
    );
    assert!(
        !test("persistence/sqlite/src/test_support.rs"),
        "undeclared"
    );
    assert!(!test("persistence/sqlite/src/approval_store.rs"));
    assert!(!test("persistence/sqlite/src/lib.rs"));
    // Cargo layout: integration tests and non-shipping examples.
    assert!(test("persistence/sqlite/tests/chain.rs"));
    assert!(test(
        "persistence/sqlite/examples/m3_line_adapter/verifier.rs"
    ));
    // A sneakily named production file is scanned as production code.
    let call_v = "let c = VerifierCapability::for_verifier_implementation();\n";
    assert_eq!(
        violations(
            "persistence/sqlite/src/sneaky_tests.rs",
            call_v,
            test("persistence/sqlite/src/sneaky_tests.rs")
        ),
        ["persistence/sqlite/src/sneaky_tests.rs:1: for_verifier_implementation"]
    );
}

#[test]
fn trusted_exports_from_the_ledger_and_approval_crates_are_flagged() {
    let flagged = |rel: &str, text: &str| violations(rel, text, false);
    let cases = [
        "pub fn cap() -> LedgerCapability {\n    todo()\n}\n",
        "pub(crate) fn cap() -> writ_broker::human::VerifierCapability {}\n",
        "pub fn assertion(\n    a: u8,\n    b: u8,\n) -> Result<VerifiedAssertion, E> {\n}\n",
        "pub fn boxed() -> Option<Box<VerifiedAssertion>>;\n",
        "pub const CAP: LedgerCapability = make();\n",
        "pub static CAP: VerifierCapability = make();\n",
        "pub unsafe fn cap() -> LedgerCapability {}\n",
    ];
    for text in cases {
        for rel in [
            "persistence/sqlite/src/approval_store.rs",
            "approval/src/verifier.rs",
        ] {
            assert_eq!(flagged(rel, text).len(), 1, "{rel}: {text}");
        }
    }
    // Not an export: private items, arguments, other types, other crates, test code.
    let fine = [
        "fn cap() -> LedgerCapability {}\n",
        "pub fn uses(c: &LedgerCapability) -> u8 {}\n",
        "pub fn other() -> Capability {}\n",
        "pub fn other() -> VerifiedAssertions {}\n",
        "pub struct Holder;\n",
    ];
    for text in fine {
        assert!(
            flagged("persistence/sqlite/src/approval_store.rs", text).is_empty(),
            "{text}"
        );
    }
    assert!(
        flagged("broker/src/human.rs", cases[0]).is_empty(),
        "the broker defines them"
    );
    assert!(violations("persistence/sqlite/src/m4_support.rs", cases[2], true).is_empty());
    let module = format!(
        "pub fn real() {{}}\n\n#[cfg(test)]\nmod tests {{\n{}}}\n",
        cases[0]
    );
    assert!(flagged("persistence/sqlite/src/lib.rs", &module).is_empty());
}
