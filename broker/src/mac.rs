//! HMAC-SHA256 tags in hex (seals and permits).
//!
//! Verification decodes the presented hex tag and compares it with the
//! expected tag in constant time (`hmac`'s `verify_slice`, backed by `subtle`).
//! A tag that is not exactly 64 hex digits never verifies.

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Length of an HMAC-SHA256 tag in bytes.
const TAG_BYTES: usize = 32;

/// Lowercase hex tag of `message` under `key`; `None` only if the HMAC
/// implementation rejects the key (it accepts every length; fail closed anyway).
pub(crate) fn sign_hex(key: &[u8], message: &[u8]) -> Option<String> {
    let mut mac = HmacSha256::new_from_slice(key).ok()?;
    mac.update(message);
    Some(hex(&mac.finalize().into_bytes()))
}

/// `true` when `tag_hex` is the tag of `message` under `key`. Upper and
/// lower case hex digits are accepted (as in the M1 reference).
pub(crate) fn verify_hex(key: &[u8], message: &[u8], tag_hex: &str) -> bool {
    let Some(tag) = unhex(tag_hex) else {
        return false;
    };
    if tag.len() != TAG_BYTES {
        return false;
    }
    let Ok(mut mac) = HmacSha256::new_from_slice(key) else {
        return false;
    };
    mac.update(message);
    mac.verify_slice(&tag).is_ok()
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(char::from(DIGITS[usize::from(b >> 4)]));
        out.push(char::from(DIGITS[usize::from(b & 0x0f)]));
    }
    out
}

fn unhex(text: &str) -> Option<Vec<u8>> {
    let (pairs, rest) = text.as_bytes().as_chunks::<2>();
    if !rest.is_empty() {
        return None;
    }
    let nibble = |c: u8| {
        char::from(c)
            .to_digit(16)
            .and_then(|d| u8::try_from(d).ok())
    };
    pairs
        .iter()
        .map(|&[high, low]| Some((nibble(high)? << 4) | nibble(low)?))
        .collect()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn rfc4231_test_case_2() {
        // RFC 4231 section 4.3: key "Jefe", data "what do ya want for nothing?".
        assert_eq!(
            sign_hex(b"Jefe", b"what do ya want for nothing?").unwrap(),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn verification_requires_the_exact_tag() {
        let key = [7u8; 32];
        let tag = sign_hex(&key, b"m").unwrap();
        assert!(verify_hex(&key, b"m", &tag));
        assert!(verify_hex(&key, b"m", &tag.to_uppercase()));
        assert!(!verify_hex(&key, b"n", &tag));
        assert!(!verify_hex(&[8u8; 32], b"m", &tag));
        assert!(!verify_hex(&key, b"m", &tag[..62]), "truncated");
        assert!(!verify_hex(&key, b"m", &format!("{tag}00")), "extended");
        assert!(!verify_hex(&key, b"m", ""));
        assert!(!verify_hex(&key, b"m", &format!("{}g", &tag[..63])));
        assert!(!verify_hex(&key, b"m", &format!("+{}", &tag[1..])));
    }
}
