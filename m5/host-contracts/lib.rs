//! Nonprivileged HostAdapter normalization against a test-only intake interface.

#![forbid(unsafe_code)]

mod claude;
mod cli;

use writ_kernel::json::{self, Map, Value};
use writ_rpc::frame::MAX_CONTROL_BYTES;

pub use claude::{claude_observation, claude_protected_write};
pub use cli::{cli_observation, cli_protected_write};

const MAX_TARGET_BYTES: usize = 4096;
const MAX_TARGET_DEPTH: usize = 64;
const MAX_COMPONENT_BYTES: usize = 255;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    BeforeTool,
    PermissionPrompt,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WriteObservation {
    pub phase: Phase,
    pub relative_path: String,
}

/// A syntactic create-only proposal. Test callers still need service-owned scope,
/// snapshot validation, authorization and protected target resolution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CreateProposal {
    pub relative_path: String,
    pub source_path: String,
    pub snapshot_id: String,
    pub manifest_digest: String,
    pub content_digest: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputError {
    Oversized,
    Malformed,
    UnknownOperation,
}

fn parse_control(bytes: &[u8]) -> Result<Map, InputError> {
    if bytes.len() > MAX_CONTROL_BYTES as usize {
        return Err(InputError::Oversized);
    }
    match json::parse(bytes) {
        Ok(Value::Obj(object)) => Ok(object),
        _ => Err(InputError::Malformed),
    }
}

fn text<'a>(object: &'a Map, key: &str) -> Result<&'a str, InputError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or(InputError::Malformed)
}

fn snapshot_id_has_storage_shape(id: &str) -> bool {
    id.strip_prefix("snap-").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|ch| ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch))
    })
}

fn snapshot_source_has_storage_shape(path: &str) -> bool {
    path.bytes().all(|ch| (0x20..=0x7e).contains(&ch))
        && path.split('/').all(|component| {
            !matches!(component, "" | "." | "..") && component.len() <= MAX_COMPONENT_BYTES
        })
}

#[cfg(test)]
mod tests;
