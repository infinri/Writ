//! Broker key material (M3-02): adapter ingress keys and the permit key.
//!
//! Keys are only ever supplied explicitly by the caller. This module has no
//! default keys and never reads the environment, a file or any other ambient
//! source; production key management, registration and storage are out of
//! scope here (M5, M9). Key bytes are never printed: `Debug` is redacted.
//! Ingress keys and the permit key are distinct types so one cannot be used
//! in place of the other.

use std::collections::BTreeMap;
use std::fmt;

/// Minimum key length in bytes (the HMAC-SHA256 output length).
pub const MIN_KEY_BYTES: usize = 32;

/// Key material rejected at construction.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyError {
    /// Fewer than [`MIN_KEY_BYTES`] bytes.
    TooShort,
}

impl fmt::Display for KeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeyError::TooShort => write!(f, "key shorter than {MIN_KEY_BYTES} bytes"),
        }
    }
}

impl std::error::Error for KeyError {}

struct Secret(Vec<u8>);

impl Secret {
    fn new(bytes: Vec<u8>) -> Result<Secret, KeyError> {
        if bytes.len() < MIN_KEY_BYTES {
            return Err(KeyError::TooShort);
        }
        Ok(Secret(bytes))
    }
}

/// HMAC-SHA256 key with which one registered adapter seals requests (D-010).
pub struct IngressKey(Secret);

impl IngressKey {
    /// A key from raw bytes (at least [`MIN_KEY_BYTES`]).
    pub fn from_bytes(bytes: Vec<u8>) -> Result<IngressKey, KeyError> {
        Secret::new(bytes).map(IngressKey)
    }

    pub(crate) fn bytes(&self) -> &[u8] {
        &self.0 .0
    }
}

impl fmt::Debug for IngressKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("IngressKey(<redacted>)")
    }
}

/// HMAC-SHA256 key with which the broker signs permits and the controlled
/// effect adapter verifies them (SR-07). Held by the broker only.
pub struct PermitKey(Secret);

impl PermitKey {
    /// A key from raw bytes (at least [`MIN_KEY_BYTES`]).
    pub fn from_bytes(bytes: Vec<u8>) -> Result<PermitKey, KeyError> {
        Secret::new(bytes).map(PermitKey)
    }

    pub(crate) fn bytes(&self) -> &[u8] {
        &self.0 .0
    }
}

impl fmt::Debug for PermitKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PermitKey(<redacted>)")
    }
}

/// Registered adapter id to ingress key lookup.
///
/// Only an adapter with a key here can seal requests. Removing an adapter's
/// key makes every seal made with it refuse from the next lookup on
/// (rotation); the broker never caches keys or verification results.
pub trait KeyRing {
    /// The key registered for `adapter_id`; `None` when the adapter is not registered.
    fn ingress_key(&self, adapter_id: &str) -> Option<&IngressKey>;
}

/// An in-memory [`KeyRing`] filled explicitly by its owner. It starts empty
/// and loads nothing by itself.
#[derive(Debug, Default)]
pub struct MemoryKeyRing {
    keys: BTreeMap<String, IngressKey>,
}

impl MemoryKeyRing {
    /// An empty key ring: every seal refuses until a key is registered.
    pub fn new() -> MemoryKeyRing {
        MemoryKeyRing::default()
    }

    /// Register `key` for `adapter_id`, replacing (and returning) any previous key.
    pub fn register(&mut self, adapter_id: &str, key: IngressKey) -> Option<IngressKey> {
        self.keys.insert(adapter_id.to_string(), key)
    }

    /// Remove the key of `adapter_id`; `true` when one was registered.
    pub fn remove(&mut self, adapter_id: &str) -> bool {
        self.keys.remove(adapter_id).is_some()
    }
}

impl KeyRing for MemoryKeyRing {
    fn ingress_key(&self, adapter_id: &str) -> Option<&IngressKey> {
        self.keys.get(adapter_id)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn short_keys_are_rejected() {
        assert_eq!(
            IngressKey::from_bytes(vec![1; MIN_KEY_BYTES - 1]).err(),
            Some(KeyError::TooShort)
        );
        assert_eq!(
            PermitKey::from_bytes(Vec::new()).err(),
            Some(KeyError::TooShort)
        );
        assert!(IngressKey::from_bytes(vec![1; MIN_KEY_BYTES]).is_ok());
        assert!(PermitKey::from_bytes(vec![1; 64]).is_ok());
    }

    #[test]
    fn debug_never_prints_key_bytes() {
        let ingress = IngressKey::from_bytes(vec![0xab; 32]).unwrap();
        let permit = PermitKey::from_bytes(vec![0xab; 32]).unwrap();
        let mut ring = MemoryKeyRing::new();
        ring.register("a", IngressKey::from_bytes(vec![0xab; 32]).unwrap());
        for text in [
            format!("{ingress:?}"),
            format!("{permit:?}"),
            format!("{ring:?}"),
        ] {
            assert!(!text.contains("171") && !text.contains("ab, ab"), "{text}");
            assert!(text.contains("redacted"), "{text}");
        }
    }

    #[test]
    fn memory_key_ring_starts_empty_and_registers_explicitly() {
        let mut ring = MemoryKeyRing::new();
        assert!(ring.ingress_key("writ-host-adapter:fixture").is_none());
        assert!(ring
            .register(
                "writ-host-adapter:fixture",
                IngressKey::from_bytes(vec![1; 32]).unwrap()
            )
            .is_none());
        assert!(ring.ingress_key("writ-host-adapter:fixture").is_some());
        assert!(ring
            .register(
                "writ-host-adapter:fixture",
                IngressKey::from_bytes(vec![2; 32]).unwrap()
            )
            .is_some());
        assert!(ring.remove("writ-host-adapter:fixture"));
        assert!(!ring.remove("writ-host-adapter:fixture"));
        assert!(ring.ingress_key("writ-host-adapter:fixture").is_none());
    }
}
