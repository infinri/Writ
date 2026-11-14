//! Trusted-value constructors in the approval channel and the ledger (M4-INTERFACES C3,
//! C4; S5, X8). Kept apart from `trusted_constructors.rs`, which confines the capability
//! constructor calls.
//!
//! In production code of `approval/` (the `writ-approval` crate) and
//! `persistence/sqlite/src/` (the SQLite ledger), every function whose return type names a
//! capability (`*Capability`), a `VerifiedAssertion` or a `VerifiedRegistration` must be one
//! of the legitimate constructors listed in [`ALLOWED`]; any other one (public, private or a
//! trait method) fails this test. Test code is decided by the declaring `mod` line: a file
//! whose `mod <name>;` declaration in its crate root carries `#[cfg(test)]`, a trailing
//! `#[cfg(test)] mod tests {` region, or an integration-test file under the crate's `tests/`
//! directory (never shipped). A file whose declaration is not found counts as production
//! (fail closed). C4: these crates may not use `build.rs`, `include!`, `include_str!` or
//! `include_bytes!` without a scanner update.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// Crate source roots scanned (production code), with their crate root file.
const ROOTS: &[(&str, &str)] = &[
    ("approval/src/", "approval/src/lib.rs"),
    ("persistence/sqlite/src/", "persistence/sqlite/src/lib.rs"),
];

/// Integration-test directories of the scanned crates (test code, never shipped).
/// Return-type names that only a legitimate constructor may produce.
const TRUSTED: &[&str] = &["Capability", "VerifiedAssertion", "VerifiedRegistration"];

/// (file, function) pairs allowed to return a trusted type: the ledger's private
/// capability helper (its own constructor calls are confined by `trusted_constructors.rs`)
/// and the ledger's registration finish, which returns only a result its own registration
/// row recorded (X8).
const ALLOWED: &[(&str, &str)] = &[
    ("persistence/sqlite/src/approval_store.rs", "cap"),
    (
        "persistence/sqlite/src/approval_store.rs",
        "finish_registration",
    ),
    (
        "persistence/sqlite/src/approval_store.rs",
        "finish_registration_as",
    ),
];

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

/// Whether module `name` is declared `#[cfg(test)]` in `root_text` (attribute lines
/// directly above `mod name;` or `pub mod name;`). `None` when no declaration is found.
fn declared_test_only(root_text: &str, name: &str) -> Option<bool> {
    let lines: Vec<&str> = root_text.lines().map(str::trim).collect();
    for (i, line) in lines.iter().enumerate() {
        let decl = line
            .strip_prefix("pub(crate) ")
            .or_else(|| line.strip_prefix("pub "))
            .unwrap_or(line);
        if decl != format!("mod {name};") {
            continue;
        }
        let attrs = lines[..i]
            .iter()
            .rev()
            .take_while(|l| l.starts_with("#[") && l.ends_with(']'));
        return Some(attrs.into_iter().any(|a| *a == "#[cfg(test)]"));
    }
    None
}

/// Every function in production `text` whose return type names a trusted type and that is
/// not allow-listed, as `path:line: name`.
fn violations(rel: &str, text: &str, test_from: Option<usize>) -> Vec<String> {
    let lines: Vec<&str> = text.lines().collect();
    let mut found = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if test_from.is_some_and(|start| i >= start) {
            break;
        }
        let code = line.trim_start();
        if code.starts_with("//") {
            continue;
        }
        let Some(at) = code
            .match_indices("fn ")
            .map(|(at, _)| at)
            .find(|&at| at == 0 || !code.as_bytes()[at - 1].is_ascii_alphanumeric())
        else {
            continue;
        };
        // The signature runs to the first `{` or `;` (possibly over several lines).
        let mut signature = String::new();
        for l in &lines[i..] {
            let l = if signature.is_empty() { &code[at..] } else { l };
            match l.find(['{', ';']) {
                Some(end) => {
                    signature.push_str(&l[..end]);
                    break;
                }
                None => {
                    signature.push_str(l);
                    signature.push(' ');
                }
            }
        }
        let name: String = signature[3..]
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        let Some((_, returns)) = signature.split_once("->") else {
            continue;
        };
        if TRUSTED.iter().any(|t| returns.contains(t))
            && !ALLOWED.iter().any(|(f, n)| *f == rel && *n == name)
        {
            found.push(format!("{rel}:{}: {name}", i + 1));
        }
    }
    found
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.is_dir() {
            walk(&path, out)?;
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
    Ok(())
}

fn workspace() -> io::Result<PathBuf> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| io::Error::other("no workspace root"))
}

#[test]
fn trusted_values_are_returned_only_by_their_legitimate_constructors() -> io::Result<()> {
    let root = workspace()?;
    let mut found = Vec::new();
    let mut allowed_seen = 0;
    for (src, crate_root) in ROOTS {
        let root_text = fs::read_to_string(root.join(crate_root))?;
        let mut files = Vec::new();
        walk(&root.join(src), &mut files)?;
        assert!(!files.is_empty(), "{src} not scanned");
        for path in files {
            let rel = path
                .strip_prefix(&root)
                .map_err(io::Error::other)?
                .to_string_lossy()
                .replace('\\', "/");
            let text = fs::read_to_string(&path)?;
            let lines: Vec<&str> = text.lines().collect();
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            let test_file = if rel == *crate_root {
                false
            } else {
                declared_test_only(&root_text, stem).unwrap_or(false)
            };
            if test_file {
                continue;
            }
            allowed_seen += ALLOWED
                .iter()
                .filter(|(f, n)| *f == rel && text.contains(&format!("fn {n}(")))
                .count();
            found.extend(violations(&rel, &text, test_module_start(&lines)));
        }
    }
    assert_eq!(
        allowed_seen,
        ALLOWED.len(),
        "the allow-list names stale functions"
    );
    assert!(
        found.is_empty(),
        "trusted values returned outside their legitimate constructors:\n{}",
        found.join("\n")
    );
    Ok(())
}

#[test]
fn no_build_scripts_or_includes_in_the_scanned_crates() -> io::Result<()> {
    let root = workspace()?;
    for dir in ["approval", "persistence/sqlite"] {
        assert!(!root.join(dir).join("build.rs").exists(), "{dir}/build.rs");
        let manifest = fs::read_to_string(root.join(dir).join("Cargo.toml"))?;
        assert!(
            !manifest.contains("build ="),
            "{dir}: build script in manifest"
        );
    }
    for (src, crate_root) in ROOTS {
        let root_text = fs::read_to_string(root.join(crate_root))?;
        let mut files = Vec::new();
        walk(&root.join(src), &mut files)?;
        for path in files {
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            let is_root = path.ends_with(Path::new(crate_root).file_name().unwrap_or_default());
            if !is_root && declared_test_only(&root_text, stem).unwrap_or(false) {
                continue;
            }
            let text = fs::read_to_string(&path)?;
            let lines: Vec<&str> = text.lines().collect();
            let end = test_module_start(&lines).unwrap_or(lines.len());
            production_includes(&path, &lines[..end]);
        }
    }
    Ok(())
}

/// Production code may include only the ledger's reviewed SQL schema files.
fn production_includes(path: &Path, lines: &[&str]) {
    for line in lines.iter().filter(|l| !l.trim_start().starts_with("//")) {
        for m in ["include!(", "include_bytes!(", "include_str!("] {
            // SQL schema files are the reviewed exception of the ledger (C4 predates M4).
            let schema = line.contains(".sql\")");
            assert!(!line.contains(m) || schema, "{}: {line}", path.display());
        }
    }
}

#[test]
fn the_scanner_flags_synthetic_violations() {
    let bad = "pub fn mint() -> VerifierCapability {\n    todo\n}\n\
               fn forge(\n    x: u8,\n) -> Result<VerifiedAssertion, E> {\n}\n\
               pub(crate) fn reg() -> Option<VerifiedRegistration>;\n\
               pub fn fine() -> u8 { let f = || -> VerifiedAssertion { x }; 1 }\n\
               // pub fn comment() -> LedgerCapability {}\n";
    assert_eq!(
        violations("approval/src/x.rs", bad, None),
        [
            "approval/src/x.rs:1: mint",
            "approval/src/x.rs:4: forge",
            "approval/src/x.rs:8: reg"
        ]
    );
    // Allowed by (file, name) only.
    let reg = "fn finish_registration(&self) -> Result<VerifiedRegistration, R> {}\n";
    assert!(violations("persistence/sqlite/src/approval_store.rs", reg, None).is_empty());
    assert_eq!(
        violations("persistence/sqlite/src/lib.rs", reg, None).len(),
        1
    );
    assert_eq!(violations("approval/src/verifier.rs", reg, None).len(), 1);
    // A trailing cfg(test) module is test code.
    let module = format!("pub fn ok() {{}}\n#[cfg(test)]\n#[allow(x)]\nmod tests {{\n{bad}}}\n");
    let lines: Vec<&str> = module.lines().collect();
    assert!(violations("approval/src/x.rs", &module, test_module_start(&lines)).is_empty());
    // Test files are decided by their declaring mod line, not by name.
    let root = "mod approval_store;\n#[cfg(test)]\n#[allow(clippy::unwrap_used)]\nmod m4_approval_tests;\npub mod page;\n";
    assert_eq!(declared_test_only(root, "m4_approval_tests"), Some(true));
    assert_eq!(declared_test_only(root, "approval_store"), Some(false));
    assert_eq!(declared_test_only(root, "page"), Some(false));
    assert_eq!(declared_test_only(root, "verifier_tests"), None);
}
