//! Identifier and code patterns (D-017): explicit, case-sensitive, compared
//! byte for byte; never case-folded or Unicode-normalized.

fn tail_ok(tail: &[u8], extra: &[u8]) -> bool {
    tail.iter()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || extra.contains(c))
}

/// `^[a-z][a-z0-9_-]{0,63}$`: criterion, workflow, state and evaluator ids.
pub fn is_name(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => s.len() <= 64 && first.is_ascii_lowercase() && tail_ok(tail, b"_-"),
        None => false,
    }
}

/// `^[a-z0-9][a-z0-9_-]{0,63}$`: tenant, project, task, record, snapshot and policy ids.
pub fn is_id(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => {
            s.len() <= 64
                && (first.is_ascii_lowercase() || first.is_ascii_digit())
                && tail_ok(tail, b"_-")
        }
        None => false,
    }
}

/// `^[0-9]{1,9}$`: acceptance policy versions.
pub fn is_policy_version(s: &str) -> bool {
    !s.is_empty() && s.len() <= 9 && s.bytes().all(|c| c.is_ascii_digit())
}

/// `^sha256:[0-9a-f]{64}$`: digests.
pub fn is_digest(s: &str) -> bool {
    match s.strip_prefix("sha256:") {
        Some(hex) => {
            hex.len() == 64
                && hex
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        }
        None => false,
    }
}

/// `^[a-z][a-z0-9_.-]*$`: action kinds and capabilities.
pub fn is_kind(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => first.is_ascii_lowercase() && tail_ok(tail, b"_.-"),
        None => false,
    }
}

/// `^[A-Z][A-Z0-9_]*$`: reason codes.
pub fn is_reason_code(s: &str) -> bool {
    match s.as_bytes().split_first() {
        Some((first, tail)) => {
            first.is_ascii_uppercase()
                && tail
                    .iter()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == b'_')
        }
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names() {
        let max = format!("a{}", "b".repeat(63));
        for ok in ["a", "accepted", "wf-1", "a_b", max.as_str()] {
            assert!(is_name(ok), "{ok}");
        }
        let long = format!("a{}", "b".repeat(64));
        for bad in [
            "",
            "1a",
            "A",
            "aB",
            "a.b",
            "a b",
            "\u{e9}",
            "-a",
            long.as_str(),
            "a\u{0}",
        ] {
            assert!(!is_name(bad), "{bad:?}");
        }
    }

    #[test]
    fn ids() {
        assert!(is_id("1") && is_id("t1") && is_id("snap-1") && is_id("0_a"));
        assert!(
            !is_id("") && !is_id("_a") && !is_id("T1") && !is_id("a/b") && !is_id(&"a".repeat(65))
        );
    }

    #[test]
    fn versions_digests_kinds_codes() {
        assert!(is_policy_version("3") && is_policy_version("123456789"));
        assert!(
            !is_policy_version("")
                && !is_policy_version("1234567890")
                && !is_policy_version("v1")
                && !is_policy_version("-1")
        );
        let d = format!("sha256:{}", "a".repeat(64));
        assert!(is_digest(&d));
        assert!(
            !is_digest(&format!("sha256:{}", "A".repeat(64)))
                && !is_digest(&format!("sha256:{}", "a".repeat(63)))
                && !is_digest(&format!("sha1:{}", "a".repeat(64)))
        );
        assert!(is_kind("workflow.transition") && is_kind("a") && is_kind("file.write-x_y"));
        assert!(!is_kind("") && !is_kind("File.write") && !is_kind(".a") && !is_kind("a b"));
        assert!(is_reason_code("DEFAULT_DENY") && is_reason_code("A1"));
        assert!(
            !is_reason_code("")
                && !is_reason_code("_A")
                && !is_reason_code("a")
                && !is_reason_code("A-B")
        );
    }
}
