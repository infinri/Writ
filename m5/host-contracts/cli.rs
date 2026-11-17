use writ_kernel::ids;

use crate::{
    parse_control, snapshot_id_has_storage_shape, snapshot_source_has_storage_shape, text,
    CreateProposal, InputError, Phase, WriteObservation, MAX_COMPONENT_BYTES, MAX_TARGET_BYTES,
    MAX_TARGET_DEPTH,
};

/// Project an advisory observation from the generic CLI's synthetic JSON shape.
pub fn cli_observation(bytes: &[u8]) -> Result<WriteObservation, InputError> {
    let fields = parse_control(bytes)?;
    let phase = match (text(&fields, "phase")?, text(&fields, "operation")?) {
        ("before", "file-write") => Phase::BeforeTool,
        ("prompt", "file-write") => Phase::PermissionPrompt,
        _ => return Err(InputError::UnknownOperation),
    };
    let path = text(&fields, "target")?;
    if !is_relative(path) {
        return Err(InputError::Malformed);
    }
    Ok(WriteObservation {
        phase,
        relative_path: path.to_owned(),
    })
}

/// Translate the generic CLI's synthetic, create-only protected request.
pub fn cli_protected_write(bytes: &[u8]) -> Result<CreateProposal, InputError> {
    let fields = parse_control(bytes)?;
    if fields.len() != 6 {
        return Err(InputError::Malformed);
    }
    if text(&fields, "command")? != "create-file" {
        return Err(InputError::UnknownOperation);
    }

    let target = text(&fields, "target")?;
    let source = text(&fields, "source")?;
    let snapshot = text(&fields, "snapshot")?;
    let manifest = text(&fields, "manifest")?;
    let content = text(&fields, "content")?;
    if !(is_relative(target)
        && snapshot_source_has_storage_shape(source)
        && snapshot_id_has_storage_shape(snapshot)
        && ids::is_digest(manifest)
        && ids::is_digest(content))
    {
        return Err(InputError::Malformed);
    }

    Ok(CreateProposal {
        relative_path: target.to_owned(),
        source_path: source.to_owned(),
        snapshot_id: snapshot.to_owned(),
        manifest_digest: manifest.to_owned(),
        content_digest: content.to_owned(),
    })
}

fn is_relative(path: &str) -> bool {
    path.len() <= MAX_TARGET_BYTES
        && !path.contains('\\')
        && path.split('/').count() <= MAX_TARGET_DEPTH
        && !path.chars().any(char::is_control)
        && path.split('/').all(|component| {
            !component.is_empty()
                && component != "."
                && component != ".."
                && component.len() <= MAX_COMPONENT_BYTES
        })
}
