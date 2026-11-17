use writ_kernel::{ids, json::Value};

use crate::{
    parse_control, snapshot_id_has_storage_shape, snapshot_source_has_storage_shape, text,
    CreateProposal, InputError, Phase, WriteObservation, MAX_COMPONENT_BYTES, MAX_TARGET_BYTES,
    MAX_TARGET_DEPTH,
};

/// Project a native write hook into an advisory diagnostic. Ignore host identity
/// and workflow claims, tool content, and host prompt choices.
pub fn claude_observation(
    bytes: &[u8],
    configured_root: &str,
) -> Result<WriteObservation, InputError> {
    let object = parse_control(bytes)?;
    let phase = match text(&object, "hook_event_name")? {
        "PreToolUse" => Phase::BeforeTool,
        "PermissionRequest" => Phase::PermissionPrompt,
        _ => return Err(InputError::UnknownOperation),
    };
    if text(&object, "tool_name")? != "Write" {
        return Err(InputError::UnknownOperation);
    }
    let input = object
        .get("tool_input")
        .and_then(Value::as_obj)
        .ok_or(InputError::Malformed)?;
    let relative_path = path_under_root(text(input, "file_path")?, configured_root)?;
    Ok(WriteObservation {
        phase,
        relative_path: relative_path.to_owned(),
    })
}

/// Accept only the synthetic protected tool shape. Native hooks and native
/// tool input have a separate entry point and cannot become effect proposals.
pub fn claude_protected_write(
    bytes: &[u8],
    configured_root: &str,
) -> Result<CreateProposal, InputError> {
    let object = parse_control(bytes)?;
    if object.len() != 2 {
        return Err(InputError::Malformed);
    }
    if text(&object, "name")? != "writ_fs_create" {
        return Err(InputError::UnknownOperation);
    }
    let arguments = object
        .get("arguments")
        .and_then(Value::as_obj)
        .ok_or(InputError::Malformed)?;
    if arguments.len() != 5 {
        return Err(InputError::Malformed);
    }
    let relative_path = path_under_root(text(arguments, "file_path")?, configured_root)?;
    let source_path = text(arguments, "source_path")?;
    let snapshot_id = text(arguments, "snapshot_id")?;
    let manifest_digest = text(arguments, "manifest_digest")?;
    let content_digest = text(arguments, "content_digest")?;
    if !snapshot_id_has_storage_shape(snapshot_id)
        || !snapshot_source_has_storage_shape(source_path)
        || !ids::is_digest(manifest_digest)
        || !ids::is_digest(content_digest)
    {
        return Err(InputError::Malformed);
    }
    Ok(CreateProposal {
        relative_path: relative_path.to_owned(),
        source_path: source_path.to_owned(),
        snapshot_id: snapshot_id.to_owned(),
        manifest_digest: manifest_digest.to_owned(),
        content_digest: content_digest.to_owned(),
    })
}

fn path_under_root<'a>(path: &'a str, root: &str) -> Result<&'a str, InputError> {
    check_relative_path(root.strip_prefix('/').ok_or(InputError::Malformed)?)?;
    let relative_path = path
        .strip_prefix(root)
        .and_then(|suffix| suffix.strip_prefix('/'))
        .ok_or(InputError::Malformed)?;
    check_relative_path(relative_path)?;
    Ok(relative_path)
}

fn check_relative_path(path: &str) -> Result<(), InputError> {
    if path.len() > MAX_TARGET_BYTES {
        return Err(InputError::Malformed);
    }
    for (depth, segment) in path.split('/').enumerate() {
        if depth >= MAX_TARGET_DEPTH
            || segment.len() > MAX_COMPONENT_BYTES
            || matches!(segment, "" | "." | "..")
            || segment.chars().any(|ch| ch == '\\' || ch.is_control())
        {
            return Err(InputError::Malformed);
        }
    }
    Ok(())
}
