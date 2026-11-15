//! Local service-owned key source (M5a-05; D-045 M5-D6; M5-INTERFACES J-3).
//!
//! [`FileKeySource`] implements [`writ_broker::keys::KeySource`] over
//! `writ-key/1` files in one protected directory (deployment:
//! `/var/lib/writ/keys`, J-1; the path and the service uid are configuration,
//! [`KeyDirConfig`]).
//!
//! **File format `writ-key/1`:** one strict JSON object (kernel parser:
//! duplicate members and every other D-017 violation refused) with exactly
//! the members `contract` (`"writ-key/1"`), `purpose` (a
//! [`KeyPurpose`] name), `key_id` (a [`KeyId`] of that purpose), `not_before`
//! and `not_after` (strict `YYYY-MM-DDTHH:MM:SSZ`, `not_before < not_after`)
//! and `material` (lowercase hex of at least 32 bytes). One key per file; the
//! file name must equal [`KeyId::file_name`] of the `key_id` inside.
//!
//! **Loading (fail closed):**
//! - The directory is reached by a descriptor walk from `/`: every component
//!   is opened with `openat2` relative to its parent's descriptor
//!   (`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`,
//!   `O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`), so no symlink is
//!   followed anywhere on the path and the checks apply to the directories
//!   actually traversed. Every ancestor: a directory owned by root or the
//!   service uid, not group- or other-writable, without a POSIX ACL. The key
//!   directory: owned by the service uid, mode exactly 0700, without an access
//!   or default ACL. Any failure is a [`DirRefusal`] (the caller reports
//!   `KeysUnavailable`).
//! - Entries are listed from the directory descriptor. Names starting with
//!   `.` or ending in `.tmp` are temporary files and are ignored (the
//!   provisioning procedure writes `.<name>` and renames). Every other entry
//!   is loaded or refused individually with a [`Diagnostic`] naming the
//!   entry and the reason, never the material; a refused key is simply
//!   absent (unknown).
//! - Each file is opened relative to the directory descriptor with
//!   `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS |
//!   RESOLVE_NO_XDEV` and `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`
//!   (a FIFO never blocks the load); `fstat` must show a regular file owned
//!   by the service uid, mode exactly 0600 or 0400, one link, at most
//!   [`MAX_KEY_FILE_BYTES`]; no POSIX ACL; the read is bounded to the maximum
//!   plus one byte and must match the `fstat` size.
//! - A key that passes is presented to the ledger's [`KeyRegistry`]
//!   ([`KeyRegistry::admit`]): an id seen before with other material or
//!   purpose, material registered under another id or purpose, and a revoked
//!   id or revoked material (under any id) are refused. An unavailable
//!   registry fails the whole load.
//!
//! **ACL check.** POSIX ACLs are detected by listing extended attribute names
//! (`flistxattr`, rustix `fs`): the presence of `system.posix_acl_access` or
//! `system.posix_acl_default` refuses, whatever the ACL says. A filesystem
//! without extended attribute support (`EOPNOTSUPP`) cannot carry ACLs; any
//! other listing failure refuses.
//!
//! **Use.** [`KeySource::key`] and [`KeySource::issuing_key`] apply purpose
//! separation, the validity windows, keys revoked in this source and the
//! issuing margin (J-3: latest `not_before` whose `not_after` is at least
//! `now + lifetime + margin`). The authoritative revocation lookup is the
//! ledger's, inside the transaction that consumes the authenticated object
//! ([`writ_broker::keys::key_at_use`]). [`FileKeySource::revoke`] records a
//! revocation in the ledger first and then stops using the key here.
//!
//! **Limitation (recorded, J-3 lead decision 3):** key material is
//! overwritten on drop on a best-effort basis (read buffer, hex text and the
//! final key buffer); copies made inside the JSON parser, by reallocation or
//! by the allocator are not reached. A reviewed `zeroize` dependency may
//! replace this later.

use rustix::fs::{flistxattr, fstat, openat2, Dir, FileType, Mode, OFlags, ResolveFlags, CWD};
use rustix::io::Errno;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::fmt;
use std::io::Read as _;
use std::os::fd::{AsFd, OwnedFd};
use std::path::{Component, Path, PathBuf};
use std::time::Duration;
use writ_broker::keys::{
    lookup, overwrite, select_issuing, Admission, KeyId, KeyPurpose, KeyRegistry, KeyRevocation,
    KeySource, KeyStatus, KeyValidity, KeysUnavailable, Material,
};
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::time::Timestamp;

/// The key file contract.
pub const KEY_FILE_CONTRACT: &str = "writ-key/1";

/// Largest key file in bytes.
pub const MAX_KEY_FILE_BYTES: u64 = 4096;

/// Default issuing margin (J-3).
pub const DEFAULT_MARGIN_SECONDS: i64 = 60;

/// Default bound on the number of non-temporary entries in the directory.
pub const DEFAULT_MAX_ENTRIES: usize = 256;

const ACL_ACCESS: &[u8] = b"system.posix_acl_access";
const ACL_DEFAULT: &[u8] = b"system.posix_acl_default";
const MEMBERS: [&str; 6] = [
    "contract",
    "key_id",
    "material",
    "not_after",
    "not_before",
    "purpose",
];

/// Where the keys are and who must own them (service configuration).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyDirConfig {
    /// Absolute, normalized path of the key directory.
    pub dir: PathBuf,
    /// The service uid (`writ`): owner of the directory and of every key file.
    pub owner_uid: u32,
    /// Issuing margin in seconds (J-3; default 60).
    pub margin_seconds: i64,
    /// Most non-temporary entries accepted; more makes the source unavailable.
    pub max_entries: usize,
    /// Test only: ancestors strictly above this directory are not checked
    /// (test directories live below a world-writable `/tmp`).
    #[cfg(test)]
    pub(crate) ancestor_base: Option<PathBuf>,
}

impl KeyDirConfig {
    /// `dir` owned by `owner_uid`, with the default margin and entry bound.
    pub fn new(dir: PathBuf, owner_uid: u32) -> KeyDirConfig {
        KeyDirConfig {
            dir,
            owner_uid,
            margin_seconds: DEFAULT_MARGIN_SECONDS,
            max_entries: DEFAULT_MAX_ENTRIES,
            #[cfg(test)]
            ancestor_base: None,
        }
    }
}

/// Why the key directory could not be used (the source is unavailable).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DirRefusal {
    /// The configured path is not absolute and normalized.
    PathNotNormalized,
    /// A path component could not be opened (`errno`; a symlink component is
    /// `ENOTDIR` or `ELOOP`, never followed).
    Open {
        /// The path up to the failing component.
        path: PathBuf,
        /// The OS error number.
        errno: i32,
    },
    /// An ancestor is not a directory owned by root or the service uid, is
    /// group- or other-writable, or carries a POSIX ACL.
    Ancestor {
        /// The ancestor.
        path: PathBuf,
        /// What failed.
        reason: &'static str,
    },
    /// The key directory is not owned by the service uid.
    Owner,
    /// The key directory's mode is not exactly 0700 (the observed mode).
    Mode(u32),
    /// The key directory carries an access or default ACL (or extended
    /// attributes could not be listed).
    Acl,
    /// The directory could not be listed.
    List(i32),
    /// More than the configured number of entries.
    TooManyEntries,
    /// The ledger's key registry was unavailable.
    Registry,
}

impl fmt::Display for DirRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DirRefusal::PathNotNormalized => {
                f.write_str("key directory path not absolute and normalized")
            }
            DirRefusal::Open { path, errno } => {
                write!(
                    f,
                    "cannot open {} without following links (errno {errno})",
                    path.display()
                )
            }
            DirRefusal::Ancestor { path, reason } => {
                write!(f, "unsafe ancestor {}: {reason}", path.display())
            }
            DirRefusal::Owner => f.write_str("key directory not owned by the service uid"),
            DirRefusal::Mode(m) => write!(f, "key directory mode {m:o}, expected 700"),
            DirRefusal::Acl => f.write_str("key directory carries an ACL"),
            DirRefusal::List(e) => write!(f, "cannot list the key directory (errno {e})"),
            DirRefusal::TooManyEntries => f.write_str("too many entries in the key directory"),
            DirRefusal::Registry => f.write_str("key registry unavailable"),
        }
    }
}

impl From<DirRefusal> for KeysUnavailable {
    fn from(_: DirRefusal) -> KeysUnavailable {
        KeysUnavailable
    }
}

/// Why one key file was refused (operator diagnostic; never the material).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refused {
    /// The name is not the file name of any key id.
    UnexpectedName,
    /// `openat2` failed (`errno`; a symlink is `ELOOP`, a socket `ENXIO`).
    Open(i32),
    /// Not a regular file (directory, FIFO, device, socket).
    NotRegular,
    /// Not owned by the service uid.
    Owner,
    /// Mode not exactly 0600 or 0400 (the observed permission bits).
    Mode(u32),
    /// More than one link (the observed count).
    LinkCount(u64),
    /// A POSIX ACL is present, or extended attributes could not be listed.
    Acl,
    /// Larger than [`MAX_KEY_FILE_BYTES`], or grew while being read.
    TooLarge,
    /// The read failed or returned other than the `fstat` size.
    Read,
    /// Not a strict JSON object with exactly the `writ-key/1` members of the
    /// right types.
    Malformed,
    /// `contract` is not `writ-key/1`.
    Contract,
    /// `key_id` is not a key id.
    KeyId,
    /// `key_id` names another purpose than `purpose`.
    PurposeMismatch,
    /// The file name is not the file name of the `key_id` inside.
    NameMismatch,
    /// The validity window is malformed or empty.
    Validity,
    /// `material` is not lowercase hex of at least 32 bytes.
    Material,
    /// The registry knows the id with other material or purpose (ids are never reused).
    IdConflict,
    /// The registry knows the material under another id or purpose.
    MaterialConflict,
    /// The id or the material was revoked.
    Revoked,
}

/// A refused directory entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Diagnostic {
    /// The entry name (lossy UTF-8).
    pub entry: String,
    /// Why it was refused.
    pub refused: Refused,
}

impl fmt::Display for Diagnostic {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "key file {:?} refused: {:?}", self.entry, self.refused)
    }
}

/// What `fstat` showed, reduced to what the checks use.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observed {
    /// The file type.
    pub file_type: FileType,
    /// Owner uid.
    pub uid: u32,
    /// Permission bits (`st_mode & 0o7777`).
    pub mode: u32,
    /// Link count.
    pub nlink: u64,
    /// Size in bytes.
    pub size: i64,
}

impl Observed {
    #[allow(clippy::useless_conversion)]
    fn of(fd: impl AsFd) -> Result<Observed, Errno> {
        let st = fstat(fd)?;
        Ok(Observed {
            file_type: FileType::from_raw_mode(st.st_mode),
            uid: st.st_uid,
            mode: st.st_mode & 0o7777,
            nlink: u64::from(st.st_nlink),
            size: st.st_size,
        })
    }
}

/// The file checks of J-3 on `fstat` facts: regular file, owner, mode,
/// link count, size, in that order.
pub fn check_file(o: &Observed, owner_uid: u32) -> Result<(), Refused> {
    if o.file_type != FileType::RegularFile {
        return Err(Refused::NotRegular);
    }
    if o.uid != owner_uid {
        return Err(Refused::Owner);
    }
    if o.mode != 0o600 && o.mode != 0o400 {
        return Err(Refused::Mode(o.mode));
    }
    if o.nlink != 1 {
        return Err(Refused::LinkCount(o.nlink));
    }
    if u64::try_from(o.size).map_or(true, |s| s > MAX_KEY_FILE_BYTES) {
        return Err(Refused::TooLarge);
    }
    Ok(())
}

/// The key-directory checks of J-3 on `fstat` facts (the ACL check is separate).
pub fn check_key_dir(o: &Observed, owner_uid: u32) -> Result<(), DirRefusal> {
    if o.file_type != FileType::Directory || o.uid != owner_uid {
        return Err(DirRefusal::Owner);
    }
    if o.mode != 0o700 {
        return Err(DirRefusal::Mode(o.mode));
    }
    Ok(())
}

/// The ancestor checks of J-3 on `fstat` facts (the ACL check is separate).
pub fn check_ancestor(o: &Observed, owner_uid: u32) -> Result<(), &'static str> {
    if o.file_type != FileType::Directory {
        return Err("not a directory");
    }
    if o.uid != 0 && o.uid != owner_uid {
        return Err("owned by neither root nor the service uid");
    }
    if o.mode & 0o022 != 0 {
        return Err("group- or other-writable");
    }
    Ok(())
}

/// `Ok(true)` when the open file carries a POSIX access or default ACL.
fn has_acl(fd: impl AsFd) -> Result<bool, Errno> {
    let mut buf = [0u8; 4096];
    let n = match flistxattr(fd, &mut buf[..]) {
        Ok(n) => n,
        Err(Errno::OPNOTSUPP) => return Ok(false),
        Err(e) => return Err(e),
    };
    Ok(buf
        .get(..n)
        .unwrap_or_default()
        .split(|b| *b == 0)
        .any(|name| name == ACL_ACCESS || name == ACL_DEFAULT))
}

const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

fn walk_resolve() -> ResolveFlags {
    ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS
}

/// Open the key directory by a descriptor walk from `/`, checking every
/// ancestor and the directory itself.
fn open_key_dir(config: &KeyDirConfig) -> Result<OwnedFd, DirRefusal> {
    let mut names: Vec<&OsStr> = Vec::new();
    let mut components = config.dir.components();
    if components.next() != Some(Component::RootDir) {
        return Err(DirRefusal::PathNotNormalized);
    }
    for c in components {
        match c {
            Component::Normal(n) => names.push(n),
            _ => return Err(DirRefusal::PathNotNormalized),
        }
    }
    // `components` silently drops `.` and repeated or trailing `/`; refuse
    // any path that is not already in that form.
    let rebuilt: PathBuf = std::iter::once(OsStr::new("/"))
        .chain(names.iter().copied())
        .collect();
    if names.is_empty() || rebuilt.as_os_str() != config.dir.as_os_str() {
        return Err(DirRefusal::PathNotNormalized);
    }
    #[cfg(test)]
    let skip = config
        .ancestor_base
        .as_ref()
        .map_or(0, |b| b.components().count().saturating_sub(1));
    #[cfg(not(test))]
    let skip = 0usize;
    let mut path = PathBuf::from("/");
    let open_err = |path: &Path, e: Errno| DirRefusal::Open {
        path: path.to_path_buf(),
        errno: e.raw_os_error(),
    };
    let mut fd = openat2(
        CWD,
        "/",
        DIR_FLAGS,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
    )
    .map_err(|e| open_err(&path, e))?;
    // Index 0 is "/", index i is the directory after `names[i - 1]`.
    let last = names.len();
    for i in 0..=last {
        if i > 0 {
            path.push(names[i - 1]);
            fd = openat2(&fd, names[i - 1], DIR_FLAGS, Mode::empty(), walk_resolve())
                .map_err(|e| open_err(&path, e))?;
        }
        let observed = Observed::of(&fd).map_err(|e| open_err(&path, e))?;
        if i == last {
            check_key_dir(&observed, config.owner_uid)?;
            if has_acl(&fd).unwrap_or(true) {
                return Err(DirRefusal::Acl);
            }
        } else if i >= skip {
            let ancestor = |reason| DirRefusal::Ancestor {
                path: path.clone(),
                reason,
            };
            check_ancestor(&observed, config.owner_uid).map_err(ancestor)?;
            if has_acl(&fd).unwrap_or(true) {
                return Err(ancestor("carries an ACL"));
            }
        }
    }
    Ok(fd)
}

fn is_temporary(name: &[u8]) -> bool {
    name.starts_with(b".") || name.ends_with(b".tmp")
}

/// Decode lowercase hex; `None` for anything else.
fn unhex(text: &[u8]) -> Option<Vec<u8>> {
    let (pairs, rest) = text.as_chunks::<2>();
    if !rest.is_empty() {
        return None;
    }
    let nibble = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    };
    let mut out = Vec::with_capacity(pairs.len());
    for &[high, low] in pairs {
        match (nibble(high), nibble(low)) {
            (Some(h), Some(l)) => out.push((h << 4) | l),
            _ => {
                overwrite(&mut out);
                return None;
            }
        }
    }
    Some(out)
}

struct Loaded {
    id: KeyId,
    material: Material,
    validity: KeyValidity,
}

/// Parse a `writ-key/1` document named `name`. The buffer is overwritten
/// by the caller.
fn parse_key(bytes: &[u8], name: &str) -> Result<Loaded, Refused> {
    let Ok(Value::Obj(mut members)) = json::parse(bytes) else {
        return Err(Refused::Malformed);
    };
    // Take the material out first so that it is overwritten on every path.
    let mut material_hex = match members.remove("material") {
        Some(Value::Str(s)) => s.into_bytes(),
        _ => return Err(Refused::Malformed),
    };
    let result = parse_members(&members, &material_hex, name);
    overwrite(&mut material_hex);
    result
}

fn parse_members(members: &json::Map, material_hex: &[u8], name: &str) -> Result<Loaded, Refused> {
    if members.len() != MEMBERS.len() - 1
        || !MEMBERS
            .iter()
            .filter(|m| **m != "material")
            .all(|m| members.get(*m).and_then(Value::as_str).is_some())
    {
        return Err(Refused::Malformed);
    }
    let text = |k: &str| members.get(k).and_then(Value::as_str).unwrap_or_default();
    if text("contract") != KEY_FILE_CONTRACT {
        return Err(Refused::Contract);
    }
    let purpose = KeyPurpose::parse(text("purpose")).ok_or(Refused::Malformed)?;
    let id = KeyId::parse(text("key_id")).ok_or(Refused::KeyId)?;
    if id.purpose() != purpose {
        return Err(Refused::PurposeMismatch);
    }
    if id.file_name() != name {
        return Err(Refused::NameMismatch);
    }
    let validity = match (
        Timestamp::parse_strict(text("not_before")),
        Timestamp::parse_strict(text("not_after")),
    ) {
        (Some(nb), Some(na)) => KeyValidity::new(nb, na).ok_or(Refused::Validity)?,
        _ => return Err(Refused::Validity),
    };
    let bytes = unhex(material_hex).ok_or(Refused::Material)?;
    let material = Material::new(purpose, bytes).map_err(|_| Refused::Material)?;
    Ok(Loaded {
        id,
        material,
        validity,
    })
}

fn read_key_file(dir: &OwnedFd, name: &str, owner_uid: u32) -> Result<Loaded, Refused> {
    let fd = openat2(
        dir,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
        walk_resolve() | ResolveFlags::NO_XDEV,
    )
    .map_err(|e| Refused::Open(e.raw_os_error()))?;
    let observed = Observed::of(&fd).map_err(|e| Refused::Open(e.raw_os_error()))?;
    check_file(&observed, owner_uid)?;
    if has_acl(&fd).unwrap_or(true) {
        return Err(Refused::Acl);
    }
    let limit = MAX_KEY_FILE_BYTES + 1;
    let mut buf = Vec::with_capacity(usize::try_from(limit).unwrap_or(4097));
    let read = std::fs::File::from(fd).take(limit).read_to_end(&mut buf);
    let result = match read {
        Err(_) => Err(Refused::Read),
        Ok(_) if buf.len() as u64 > MAX_KEY_FILE_BYTES => Err(Refused::TooLarge),
        Ok(n) if i64::try_from(n).ok() != Some(observed.size) => Err(Refused::Read),
        Ok(_) => parse_key(&buf, name),
    };
    overwrite(&mut buf);
    result
}

/// Outcome of [`FileKeySource::revoke`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RevokeError {
    /// No key with this id is loaded (its material is not known here).
    Unknown,
    /// The registry could not record the revocation; nothing changed here.
    Unavailable,
}

/// The `writ-key/1` file key source (module documentation).
#[derive(Debug)]
pub struct FileKeySource {
    keys: BTreeMap<KeyId, (Material, KeyValidity)>,
    revoked: BTreeSet<KeyId>,
    margin_seconds: i64,
    diagnostics: Vec<Diagnostic>,
}

impl FileKeySource {
    /// Load every key in `config.dir`, admitting each through `registry`.
    /// Per-file refusals are [`FileKeySource::diagnostics`]; a directory
    /// refusal or an unavailable registry is a [`DirRefusal`] (the caller
    /// reports `KeysUnavailable`).
    pub fn load(
        config: &KeyDirConfig,
        registry: &mut dyn KeyRegistry,
    ) -> Result<FileKeySource, DirRefusal> {
        let dir = open_key_dir(config)?;
        let mut names: Vec<Vec<u8>> = Vec::new();
        let listing = Dir::read_from(&dir).map_err(|e| DirRefusal::List(e.raw_os_error()))?;
        for entry in listing {
            let entry = entry.map_err(|e| DirRefusal::List(e.raw_os_error()))?;
            let name = entry.file_name().to_bytes();
            if name == b"." || name == b".." || is_temporary(name) {
                continue;
            }
            if names.len() == config.max_entries {
                return Err(DirRefusal::TooManyEntries);
            }
            names.push(name.to_vec());
        }
        names.sort();
        let mut source = FileKeySource {
            keys: BTreeMap::new(),
            revoked: BTreeSet::new(),
            margin_seconds: config.margin_seconds,
            diagnostics: Vec::new(),
        };
        for raw in names {
            let entry = String::from_utf8_lossy(&raw).into_owned();
            let refuse = |refused| Diagnostic {
                entry: entry.clone(),
                refused,
            };
            let name = match std::str::from_utf8(&raw) {
                Ok(n) if KeyId::from_file_name(n).is_some() => n,
                _ => {
                    source.diagnostics.push(refuse(Refused::UnexpectedName));
                    continue;
                }
            };
            let loaded = match read_key_file(&dir, name, config.owner_uid) {
                Ok(l) => l,
                Err(r) => {
                    source.diagnostics.push(refuse(r));
                    continue;
                }
            };
            let admission = registry
                .admit(
                    &loaded.id,
                    loaded.material.purpose(),
                    loaded.material.digest(),
                )
                .map_err(|_| DirRefusal::Registry)?;
            match admission {
                Admission::Admitted => {
                    source
                        .keys
                        .insert(loaded.id, (loaded.material, loaded.validity));
                }
                Admission::IdConflict => source.diagnostics.push(refuse(Refused::IdConflict)),
                Admission::MaterialConflict => {
                    source.diagnostics.push(refuse(Refused::MaterialConflict))
                }
                Admission::Revoked => source.diagnostics.push(refuse(Refused::Revoked)),
            }
        }
        Ok(source)
    }

    /// Entries refused at load (operator diagnostics; never material).
    pub fn diagnostics(&self) -> &[Diagnostic] {
        &self.diagnostics
    }

    /// The loaded key ids with their validity windows.
    pub fn loaded(&self) -> impl Iterator<Item = (&KeyId, &KeyValidity)> {
        self.keys.iter().map(|(id, (_, v))| (id, v))
    }

    /// Revoke loaded key `id`: record the revocation (id, material digest,
    /// purpose) in `registry` first, then stop using the key here. Revoked
    /// material stays refused under any id at every later load.
    pub fn revoke(
        &mut self,
        registry: &mut dyn KeyRegistry,
        id: &KeyId,
        revoked_at: Timestamp,
        actor: &str,
        reason: &str,
    ) -> Result<KeyRevocation, RevokeError> {
        let (material, _) = self.keys.get(id).ok_or(RevokeError::Unknown)?;
        let revocation = KeyRevocation {
            key_id: id.clone(),
            material: material.digest().clone(),
            purpose: material.purpose(),
            revoked_at,
            actor: actor.to_string(),
            reason: reason.to_string(),
        };
        registry
            .revoke(revocation.clone())
            .map_err(|_| RevokeError::Unavailable)?;
        self.revoked.insert(id.clone());
        Ok(revocation)
    }
}

impl KeySource for FileKeySource {
    fn key(
        &self,
        purpose: KeyPurpose,
        id: &KeyId,
        now: Timestamp,
    ) -> Result<KeyStatus<'_, Material>, KeysUnavailable> {
        Ok(lookup(
            self.keys.get(id).map(|(m, v)| (m, v)),
            self.revoked.contains(id),
            purpose,
            now,
        ))
    }

    fn issuing_key(
        &self,
        purpose: KeyPurpose,
        now: Timestamp,
        lifetime: Duration,
    ) -> Result<(KeyId, &Material), KeysUnavailable> {
        select_issuing(
            self.keys
                .iter()
                .map(|(id, (m, v))| (id, m, v, self.revoked.contains(id))),
            purpose,
            now,
            lifetime,
            self.margin_seconds,
        )
    }
}

#[cfg(test)]
mod tests;
