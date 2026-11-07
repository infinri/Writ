//! Hand-written matchers for the identifier and code patterns (byte for
//! byte, no case folding, no Unicode normalization).

fn rest_ok(b: &[u8], extra: &[u8]) -> bool {
    b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || extra.contains(c))
}

/// `^[a-z][a-z0-9_-]{0,63}$`: criterion, workflow, state and evaluator ids.
pub fn is_name(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 64 && b[0].is_ascii_lowercase() && rest_ok(&b[1..], b"_-")
}

/// `^[a-z0-9][a-z0-9_-]{0,63}$`: tenant, project, task, record, snapshot, policy ids.
pub fn is_id(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 64 && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit()) && rest_ok(&b[1..], b"_-")
}

/// `^[0-9]{1,9}$`
pub fn is_policy_version(s: &str) -> bool {
    !s.is_empty() && s.len() <= 9 && s.bytes().all(|c| c.is_ascii_digit())
}

/// `^sha256:[0-9a-f]{64}$`
pub fn is_digest(s: &str) -> bool {
    s.len() == 71 && s.starts_with("sha256:") && s[7..].bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// `^[a-z][a-z0-9_.-]*$`: action kinds and capabilities.
pub fn is_kind(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b[0].is_ascii_lowercase() && rest_ok(&b[1..], b"_.-")
}

/// `^[A-Z][A-Z0-9_]*$`: reason codes.
pub fn is_reason_code(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b[0].is_ascii_uppercase() && b[1..].iter().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == b'_')
}
