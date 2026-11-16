//! The fixed bubblewrap profile `writ-bwrap/1` (J-5) and pinned runtime profiles.
//!
//! The argument vector is built only from the fixed template, the allow-listed runtime
//! profile's root-owned contents and the job's own `work/`. Request fields reach it in exactly
//! two places, both unprivileged: `--setenv NAME VALUE` (names already validated) and the
//! program arguments after `--`. No request field adds a bind, namespace, capability or option.
//!
//! A runtime profile is the directory `<runtime_root>/<id>/`: each top-level directory is bound
//! read-only at `/<name>` and each top-level symbolic link recreated with `--symlink`. Its
//! manifest digest covers every entry below it (path, type, mode, owner, content digest or link
//! target) and is recomputed before each launch (J-11.4 step 1).

use crate::protocol::Request;
use std::ffi::OsString;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;
use writ_kernel::json::{self, s, Digest, Value};

/// Profile name.
pub const PROFILE: &str = "writ-bwrap/1";

/// Top-level names a runtime profile may not use (they belong to the sandbox layout).
pub const RESERVED: [&str; 4] = ["work", "tmp", "proc", "dev"];

/// Largest runtime file hashed.
pub const MANIFEST_FILE_MAX: u64 = 64 << 20;
/// Largest runtime profile in bytes.
pub const MANIFEST_TOTAL_MAX: u64 = 2 << 30;
/// Most runtime profile entries.
pub const MANIFEST_ENTRIES_MAX: usize = 200_000;

const FIXED_HEAD: [&str; 14] = [
    "--unshare-user",
    "--unshare-ipc",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-uts",
    "--unshare-cgroup",
    "--disable-userns",
    "--new-session",
    "--die-with-parent",
    "--cap-drop",
    "ALL",
    "--hostname",
    "writ-job",
    "--clearenv",
];

const FIXED_TAIL: [&str; 14] = [
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--tmpfs",
    "/tmp",
    "--chdir",
    "/work",
    "--block-fd",
    "3",
    "--json-status-fd",
    "4",
    "--remount-ro",
    "/",
];

/// D-006 digest of the fixed template (`bwrap_profile_digest`).
pub fn profile_digest() -> String {
    let arr = |xs: &[&str]| Value::Arr(xs.iter().map(|x| s(x)).collect());
    json::digest(&json::obj([
        ("contract", s(PROFILE)),
        ("head", arr(&FIXED_HEAD)),
        (
            "runtime",
            s("--ro-bind <runtime>/<dir> /<dir>; --symlink <target> /<link>"),
        ),
        ("tail", arr(&FIXED_TAIL)),
        ("work", s("--bind <job>/work /work")),
        ("command", s("-- /work/<program> <args>")),
        ("env", s("--setenv <name> <value>")),
    ]))
    .map(|d| d.as_str().to_string())
    .unwrap_or_default()
}

/// The top level of a runtime profile.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Top {
    /// Directories bound read-only at `/<name>`.
    pub dirs: Vec<String>,
    /// Symbolic links `(name, target)` recreated at `/<name>`.
    pub links: Vec<(String, String)>,
}

fn valid_top_name(n: &str) -> bool {
    !n.is_empty()
        && !n.starts_with('.')
        && n.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-')
        && !RESERVED.contains(&n)
}

/// One manifest entry.
fn entry(path: &str, kind: &str, meta: &std::fs::Metadata, extra: (&str, String)) -> Value {
    json::obj([
        ("path", s(path)),
        ("kind", s(kind)),
        (
            "mode",
            Value::Int(i64::from(meta.permissions().mode() & 0o7777)),
        ),
        ("uid", Value::Int(i64::from(meta.uid()))),
        (extra.0, s(&extra.1)),
    ])
}

/// Walk `root` iteratively, require every entry root-owned (`owner_uid`) and not writable by
/// group or others, and return the top level and the manifest digest.
pub fn manifest(root: &Path, owner_uid: u32) -> Result<(Top, String), String> {
    let meta = std::fs::symlink_metadata(root).map_err(|e| format!("runtime root: {e}"))?;
    if !meta.is_dir() || meta.uid() != owner_uid || meta.mode() & 0o022 != 0 {
        return Err("runtime root not an owner-only-writable directory".into());
    }
    let mut top = Top::default();
    let mut entries: Vec<Value> = Vec::new();
    let mut total = 0u64;
    let mut stack = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, rel)) = stack.pop() {
        let mut names: Vec<_> = std::fs::read_dir(&dir)
            .map_err(|e| format!("{rel}: {e}"))?
            .map(|e| e.map(|e| e.file_name()))
            .collect::<Result<_, _>>()
            .map_err(|e| format!("{rel}: {e}"))?;
        names.sort();
        for name in names {
            let n = name
                .to_str()
                .ok_or_else(|| format!("{rel}: non-UTF-8 name"))?
                .to_string();
            let p = if rel.is_empty() {
                n.clone()
            } else {
                format!("{rel}/{n}")
            };
            let full = dir.join(&name);
            let m = std::fs::symlink_metadata(&full).map_err(|e| format!("{p}: {e}"))?;
            if m.uid() != owner_uid {
                return Err(format!("{p}: not owned by {owner_uid}"));
            }
            let ft = m.file_type();
            if !ft.is_symlink() && m.mode() & 0o022 != 0 {
                return Err(format!("{p}: writable by group or others"));
            }
            if entries.len() >= MANIFEST_ENTRIES_MAX {
                return Err("runtime profile has too many entries".into());
            }
            if ft.is_dir() {
                if rel.is_empty() {
                    if !valid_top_name(&n) {
                        return Err(format!("{p}: top-level name not allowed"));
                    }
                    top.dirs.push(n.clone());
                }
                entries.push(entry(&p, "dir", &m, ("digest", String::new())));
                stack.push((full, p));
            } else if ft.is_symlink() {
                let t = std::fs::read_link(&full).map_err(|e| format!("{p}: {e}"))?;
                let t = t
                    .to_str()
                    .ok_or_else(|| format!("{p}: non-UTF-8 link"))?
                    .to_string();
                if rel.is_empty() {
                    if !valid_top_name(&n) {
                        return Err(format!("{p}: top-level name not allowed"));
                    }
                    top.links.push((n.clone(), t.clone()));
                }
                entries.push(entry(&p, "link", &m, ("target", t)));
            } else if ft.is_file() {
                if rel.is_empty() {
                    return Err(format!("{p}: regular file at the top level"));
                }
                if m.len() > MANIFEST_FILE_MAX {
                    return Err(format!("{p}: larger than the manifest file bound"));
                }
                let bytes = std::fs::read(&full).map_err(|e| format!("{p}: {e}"))?;
                total += bytes.len() as u64;
                if total > MANIFEST_TOTAL_MAX {
                    return Err("runtime profile larger than its bound".into());
                }
                let d = Digest::of_bytes(&bytes).as_str().to_string();
                entries.push(entry(&p, "file", &m, ("digest", d)));
            } else {
                return Err(format!("{p}: neither directory, file nor link"));
            }
        }
    }
    entries.sort_by(|a, b| {
        a.get("path")
            .and_then(Value::as_str)
            .cmp(&b.get("path").and_then(Value::as_str))
    });
    let digest = json::digest(&json::obj([
        ("contract", s("writ-runtime-manifest/1")),
        ("entries", Value::Arr(entries)),
    ]))
    .map_err(|_| "manifest outside the D-006 domain".to_string())?;
    Ok((top, digest.as_str().to_string()))
}

/// The bubblewrap arguments (without argv[0]) for `req` with the runtime profile at
/// `runtime` (its validated top level `top`) and the job's `work` directory.
pub fn args(runtime: &Path, top: &Top, work: &Path, req: &Request) -> Vec<OsString> {
    let mut a: Vec<OsString> = FIXED_HEAD.iter().map(OsString::from).collect();
    for (k, v) in &req.env {
        a.extend(["--setenv".into(), k.into(), v.into()]);
    }
    for d in &top.dirs {
        a.push("--ro-bind".into());
        a.push(runtime.join(d).into_os_string());
        a.push(format!("/{d}").into());
    }
    for (l, t) in &top.links {
        a.extend(["--symlink".into(), t.into(), format!("/{l}").into()]);
    }
    a.extend(["--bind".into(), work.as_os_str().to_owned(), "/work".into()]);
    a.extend(FIXED_TAIL.iter().map(OsString::from));
    a.push("--".into());
    a.push(format!("/work/{}", req.program).into());
    a.extend(req.args.iter().map(OsString::from));
    a
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::testutil::Scratch;
    use std::fs;

    fn req(env: Vec<(String, String)>, args: Vec<String>) -> Request {
        Request::new(
            "run-1",
            "py",
            "bin/check",
            args,
            env,
            crate::protocol::tests::limits(),
        )
        .unwrap()
    }

    #[test]
    fn request_fields_never_become_options() {
        let top = Top {
            dirs: vec!["usr".into()],
            links: vec![("bin".into(), "usr/bin".into())],
        };
        let hostile = vec!["--bind".to_string(), "/".into(), "/".into()];
        let r = req(vec![("X".into(), "--bind / /".into())], hostile.clone());
        let a = args(
            Path::new("/usr/lib/writ-runtime/py"),
            &top,
            Path::new("/j/work"),
            &r,
        );
        let sep = a.iter().position(|x| x == "--").unwrap();
        // After the separator: the program and its arguments, verbatim.
        assert_eq!(a[sep + 1], "/work/bin/check");
        assert_eq!(
            a[sep + 2..]
                .iter()
                .map(|x| x.to_str().unwrap().to_string())
                .collect::<Vec<_>>(),
            hostile
        );
        // Before it: the env value appears only as the value of --setenv.
        let i = a.iter().position(|x| x == "--bind / /").unwrap();
        assert_eq!(a[i - 2], "--setenv");
        assert_eq!(a[i - 1], "X");
        // Exactly one writable bind: the job's own work directory.
        let binds: Vec<usize> = a[..sep]
            .iter()
            .enumerate()
            .filter(|(_, x)| *x == "--bind")
            .map(|(i, _)| i)
            .collect();
        assert_eq!(binds.len(), 1);
        assert_eq!(a[binds[0] + 1], "/j/work");
        for opt in [
            "--disable-userns",
            "--unshare-net",
            "--clearenv",
            "--block-fd",
            "--json-status-fd",
        ] {
            assert!(a[..sep].iter().any(|x| x == opt), "{opt}");
        }
    }

    #[test]
    fn profile_digest_is_stable() {
        assert_eq!(profile_digest(), profile_digest());
        assert!(crate::config::is_digest(&profile_digest()));
    }

    #[test]
    fn manifest_covers_content_and_rejects_writable_or_foreign_entries() {
        let s = Scratch::new("runtime");
        let root = s.path().join("py");
        fs::create_dir_all(root.join("usr/bin")).unwrap();
        fs::write(root.join("usr/bin/tool"), b"v1").unwrap();
        std::os::unix::fs::symlink("usr/bin", root.join("bin")).unwrap();
        for p in [root.clone(), root.join("usr"), root.join("usr/bin")] {
            fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::set_permissions(root.join("usr/bin/tool"), fs::Permissions::from_mode(0o755)).unwrap();
        let me = rustix::process::getuid().as_raw();
        let (top, d1) = manifest(&root, me).unwrap();
        assert_eq!(top.dirs, vec!["usr".to_string()]);
        assert_eq!(top.links, vec![("bin".to_string(), "usr/bin".to_string())]);
        fs::write(root.join("usr/bin/tool"), b"v2").unwrap();
        let (_, d2) = manifest(&root, me).unwrap();
        assert_ne!(d1, d2, "content change changes the digest");
        fs::set_permissions(root.join("usr/bin/tool"), fs::Permissions::from_mode(0o775)).unwrap();
        assert!(manifest(&root, me).unwrap_err().contains("writable"));
        fs::set_permissions(root.join("usr/bin/tool"), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(manifest(&root, me.wrapping_add(1)).is_err());
        fs::create_dir(root.join("work")).unwrap();
        fs::set_permissions(root.join("work"), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(manifest(&root, me).unwrap_err().contains("not allowed"));
    }
}
