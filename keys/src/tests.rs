//! Loading refusals, rotation and issuing selection on real directories under
//! the process temporary directory (the ancestor checks start at each test's
//! own base directory, `KeyDirConfig::ancestor_base`; the full walk from `/`
//! is exercised by `the_full_walk_refuses_a_world_writable_ancestor`).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use super::*;
use rustix::fs::{fsetxattr, mknodat, XattrFlags};
use std::fs;
use std::os::unix::fs::{DirBuilderExt as _, MetadataExt as _, PermissionsExt as _};
use std::sync::atomic::{AtomicUsize, Ordering};
use writ_broker::keys::{MaterialDigest, MemoryKeyRegistry};

const ACTOR: OsActor = OsActor { uid: 0, pid: 1 };

static COUNTER: AtomicUsize = AtomicUsize::new(0);

const NB: &str = "2026-11-01T00:00:00Z";
const NA: &str = "2026-12-01T00:00:00Z";

fn t(s: &str) -> Timestamp {
    Timestamp::parse_strict(s).unwrap()
}

fn id(s: &str) -> KeyId {
    KeyId::parse(s).unwrap()
}

/// 32 bytes derived at run time (never a constant in the repository), so a
/// scan for their hex in output is meaningful.
fn canary(seed: u8) -> Vec<u8> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .subsec_nanos();
    let mix = nanos ^ std::process::id().rotate_left(16);
    (0..32u32)
        .map(|i| (mix.rotate_left(i) as u8) ^ seed ^ (i as u8).wrapping_mul(37))
        .collect()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

struct Fixture {
    base: PathBuf,
    dir: PathBuf,
    uid: u32,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::set_permissions(&self.dir, fs::Permissions::from_mode(0o700));
        let _ = fs::remove_dir_all(&self.base);
    }
}

fn mkdir(path: &Path, mode: u32) {
    fs::DirBuilder::new().mode(mode).create(path).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

impl Fixture {
    fn new(tag: &str) -> Fixture {
        let base = std::env::temp_dir().join(format!(
            "writ-keys-test-{}-{}-{tag}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        mkdir(&base, 0o700);
        let dir = base.join("keys");
        mkdir(&dir, 0o700);
        let uid = fs::metadata(&dir).unwrap().uid();
        Fixture { base, dir, uid }
    }

    fn config(&self) -> KeyDirConfig {
        let mut c = KeyDirConfig::new(self.dir.clone(), self.uid);
        c.ancestor_base = Some(self.base.clone());
        c
    }

    fn write_raw(&self, name: &str, text: &str, mode: u32) -> PathBuf {
        let p = self.dir.join(name);
        fs::write(&p, text).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(mode)).unwrap();
        p
    }

    fn doc(purpose: &str, key_id: &str, nb: &str, na: &str, material: &[u8]) -> String {
        format!(
            r#"{{"contract":"writ-key/1","purpose":"{purpose}","key_id":"{key_id}","not_before":"{nb}","not_after":"{na}","material":"{}"}}"#,
            hex(material)
        )
    }

    /// A well-formed key file for `key_id` (purpose from the id), mode 0600.
    fn key(&self, key_id: &str, material: &[u8]) -> PathBuf {
        self.key_window(key_id, NB, NA, material)
    }

    fn key_window(&self, key_id: &str, nb: &str, na: &str, material: &[u8]) -> PathBuf {
        let k = id(key_id);
        self.write_raw(
            &k.file_name(),
            &Fixture::doc(k.purpose().as_str(), key_id, nb, na, material),
            0o600,
        )
    }

    fn load(&self) -> (Result<FileKeySource, DirRefusal>, MemoryKeyRegistry) {
        let mut reg = MemoryKeyRegistry::new();
        (FileKeySource::load(&self.config(), &mut reg), reg)
    }

    fn load_ok(&self) -> FileKeySource {
        self.load().0.unwrap()
    }
}

fn refusal_of(source: &FileKeySource, entry: &str) -> Option<Refused> {
    source
        .diagnostics()
        .iter()
        .find(|d| d.entry == entry)
        .map(|d| d.refused)
}

fn now() -> Timestamp {
    t("2026-11-15T00:00:00Z")
}

fn active(source: &FileKeySource, key_id: &str) -> bool {
    let k = id(key_id);
    matches!(source.key(k.purpose(), &k, now()), Ok(KeyStatus::Active(_)))
}

#[test]
fn loads_a_valid_key_and_its_material() {
    let f = Fixture::new("valid");
    let m = canary(1);
    f.key("permit/k1", &m);
    let ro = f.key("ingress/a", &canary(2));
    fs::set_permissions(ro, fs::Permissions::from_mode(0o400)).unwrap();
    let s = f.load_ok();
    assert!(s.diagnostics().is_empty(), "{:?}", s.diagnostics());
    assert!(active(&s, "permit/k1"));
    assert!(active(&s, "ingress/a"), "mode 0400 accepted");
    let k = id("permit/k1");
    let KeyStatus::Active(mat) = s.key(KeyPurpose::Permit, &k, now()).unwrap() else {
        panic!("not active")
    };
    assert_eq!(mat.digest(), &MaterialDigest::of(&m));
    assert!(mat.permit().is_some());
    assert_eq!(s.loaded().count(), 2);
}

#[test]
fn symlinked_key_file_is_refused() {
    let f = Fixture::new("symlink");
    let target = f.base.join("elsewhere.json");
    fs::write(
        &target,
        Fixture::doc("permit", "permit/k1", NB, NA, &canary(1)),
    )
    .unwrap();
    fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
    std::os::unix::fs::symlink(&target, f.dir.join("permit_k1.json")).unwrap();
    let s = f.load_ok();
    assert_eq!(
        refusal_of(&s, "permit_k1.json"),
        Some(Refused::Open(Errno::LOOP.raw_os_error()))
    );
    assert!(!active(&s, "permit/k1"));
}

#[test]
fn loose_modes_are_refused() {
    let f = Fixture::new("mode");
    for (label, mode) in [
        ("a", 0o644),
        ("b", 0o640),
        ("c", 0o604),
        ("d", 0o700),
        ("e", 0o4600),
    ] {
        let p = f.key(&format!("permit/{label}"), &canary(label.as_bytes()[0]));
        fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
    }
    let s = f.load_ok();
    for (label, mode) in [
        ("a", 0o644),
        ("b", 0o640),
        ("c", 0o604),
        ("d", 0o700),
        ("e", 0o4600),
    ] {
        assert_eq!(
            refusal_of(&s, &format!("permit_{label}.json")),
            Some(Refused::Mode(mode)),
            "{mode:o}"
        );
    }
    assert_eq!(s.loaded().count(), 0);
}

#[test]
fn hard_linked_key_file_is_refused() {
    let f = Fixture::new("nlink");
    let p = f.key("permit/k1", &canary(1));
    fs::hard_link(&p, f.base.join("second-link")).unwrap();
    let s = f.load_ok();
    assert_eq!(
        refusal_of(&s, "permit_k1.json"),
        Some(Refused::LinkCount(2))
    );
}

#[test]
fn non_regular_entries_are_refused_without_blocking() {
    let f = Fixture::new("nonregular");
    // A FIFO: opening it with O_NONBLOCK does not wait for a writer.
    let dirfd =
        rustix::fs::open(&f.dir, OFlags::RDONLY | OFlags::DIRECTORY, Mode::empty()).unwrap();
    mknodat(
        &dirfd,
        "permit_fifo.json",
        FileType::Fifo,
        Mode::from_raw_mode(0o600),
        0,
    )
    .unwrap();
    mkdir(&f.dir.join("permit_subdir.json"), 0o700);
    let s = f.load_ok();
    assert_eq!(
        refusal_of(&s, "permit_fifo.json"),
        Some(Refused::NotRegular)
    );
    assert_eq!(
        refusal_of(&s, "permit_subdir.json"),
        Some(Refused::NotRegular)
    );
}

#[test]
fn oversize_key_file_is_refused() {
    let f = Fixture::new("oversize");
    let doc = Fixture::doc("permit", "permit/k1", NB, NA, &canary(1));
    let padded = format!("{doc}{}", " ".repeat(5000));
    f.write_raw("permit_k1.json", &padded, 0o600);
    let s = f.load_ok();
    assert_eq!(refusal_of(&s, "permit_k1.json"), Some(Refused::TooLarge));
    // Exactly at the bound is accepted (whitespace is valid JSON padding).
    let at_bound = format!("{doc}{}", " ".repeat(4096 - doc.len()));
    f.write_raw(
        "permit_k2.json",
        &at_bound.replace("permit/k1", "permit/k2"),
        0o600,
    );
    let s = f.load_ok();
    assert!(active(&s, "permit/k2"), "{:?}", s.diagnostics());
}

#[test]
fn file_name_must_match_the_key_id() {
    let f = Fixture::new("name");
    let doc = Fixture::doc("permit", "permit/b", NB, NA, &canary(1));
    f.write_raw("permit_a.json", &doc, 0o600);
    f.write_raw("notakey.json", &doc, 0o600);
    f.write_raw("permit_b.JSON", &doc, 0o600);
    let s = f.load_ok();
    assert_eq!(refusal_of(&s, "permit_a.json"), Some(Refused::NameMismatch));
    assert_eq!(
        refusal_of(&s, "notakey.json"),
        Some(Refused::UnexpectedName)
    );
    assert_eq!(
        refusal_of(&s, "permit_b.JSON"),
        Some(Refused::UnexpectedName)
    );
    assert!(!active(&s, "permit/a") && !active(&s, "permit/b"));
}

#[test]
fn temporary_files_are_ignored() {
    let f = Fixture::new("temp");
    let doc = Fixture::doc("permit", "permit/k1", NB, NA, &canary(1));
    f.write_raw(".permit_k1.json", &doc, 0o644);
    f.write_raw("permit_k1.json.tmp", &doc, 0o644);
    let s = f.load_ok();
    assert!(s.diagnostics().is_empty(), "{:?}", s.diagnostics());
    assert_eq!(s.loaded().count(), 0);
}

#[test]
fn malformed_documents_are_refused() {
    let f = Fixture::new("malformed");
    let m = hex(&canary(1));
    let cases: [(&str, String, Refused); 10] = [
        ("permit_a.json", "not json".into(), Refused::Malformed),
        (
            "permit_b.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/b","not_before":"{NB}","not_after":"{NA}","material":"{m}","material":"{m}"}}"#
            ),
            Refused::Malformed,
        ),
        (
            "permit_c.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/c","not_before":"{NB}","not_after":"{NA}","material":"{m}","extra":"x"}}"#
            ),
            Refused::Malformed,
        ),
        (
            "permit_d.json",
            format!(
                r#"{{"contract":"writ-key/2","purpose":"permit","key_id":"permit/d","not_before":"{NB}","not_after":"{NA}","material":"{m}"}}"#
            ),
            Refused::Contract,
        ),
        (
            "permit_e.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/e","not_before":"{NA}","not_after":"{NB}","material":"{m}"}}"#
            ),
            Refused::Validity,
        ),
        (
            "permit_f.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/f","not_before":"2026-11-01T00:00:00.5Z","not_after":"{NA}","material":"{m}"}}"#
            ),
            Refused::Validity,
        ),
        (
            "permit_g.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/g","not_before":"{NB}","not_after":"{NA}","material":"{}"}}"#,
                &m[..62]
            ),
            Refused::Material,
        ),
        (
            "permit_h.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/h","not_before":"{NB}","not_after":"{NA}","material":"{}"}}"#,
                m.to_uppercase()
            ),
            Refused::Material,
        ),
        (
            "permit_i.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"permit/i","not_before":"{NB}","not_after":"{NA}","material":1}}"#
            ),
            Refused::Malformed,
        ),
        (
            "permit_j.json",
            format!(
                r#"{{"contract":"writ-key/1","purpose":"permit","key_id":"Permit/J","not_before":"{NB}","not_after":"{NA}","material":"{m}"}}"#
            ),
            Refused::KeyId,
        ),
    ];
    for (name, text, _) in &cases {
        f.write_raw(name, text, 0o600);
    }
    let s = f.load_ok();
    for (name, _, refused) in &cases {
        assert_eq!(refusal_of(&s, name), Some(*refused), "{name}");
    }
    assert_eq!(s.loaded().count(), 0);
}

#[test]
fn purposes_are_separated() {
    let f = Fixture::new("purpose");
    // The purpose field and the id's purpose disagree.
    f.write_raw(
        "permit_x.json",
        &Fixture::doc("ingress", "permit/x", NB, NA, &canary(1)),
        0o600,
    );
    f.key("ingress/a", &canary(2));
    let s = f.load_ok();
    assert_eq!(
        refusal_of(&s, "permit_x.json"),
        Some(Refused::PurposeMismatch)
    );
    // A key is found only under its own purpose.
    let a = id("ingress/a");
    assert!(matches!(
        s.key(KeyPurpose::Ingress, &a, now()),
        Ok(KeyStatus::Active(_))
    ));
    for p in [
        KeyPurpose::Permit,
        KeyPurpose::AcceptanceRecord,
        KeyPurpose::RunnerAttestation,
    ] {
        assert!(matches!(s.key(p, &a, now()), Ok(KeyStatus::Unknown)), "{p}");
    }
    assert!(s
        .issuing_key(KeyPurpose::Permit, now(), Duration::from_secs(30))
        .is_err());
}

#[test]
fn validity_windows_apply_at_use() {
    let f = Fixture::new("window");
    f.key_window(
        "permit/old",
        "2026-10-01T00:00:00Z",
        "2026-11-01T00:00:00Z",
        &canary(1),
    );
    f.key_window(
        "permit/later",
        "2026-12-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        &canary(2),
    );
    let s = f.load_ok();
    assert!(s.diagnostics().is_empty(), "loaded; refused only at use");
    assert!(matches!(
        s.key(KeyPurpose::Permit, &id("permit/old"), now()),
        Ok(KeyStatus::Expired)
    ));
    assert!(matches!(
        s.key(KeyPurpose::Permit, &id("permit/later"), now()),
        Ok(KeyStatus::NotYetValid)
    ));
    assert!(matches!(
        s.key(KeyPurpose::Permit, &id("permit/none"), now()),
        Ok(KeyStatus::Unknown)
    ));
    assert!(s
        .issuing_key(KeyPurpose::Permit, now(), Duration::from_secs(30))
        .is_err());
}

#[test]
fn revoked_ids_and_revoked_material_are_refused_at_load() {
    let f = Fixture::new("revoked");
    let m = canary(1);
    f.key("permit/k1", &m);
    let mut reg = MemoryKeyRegistry::new();
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    assert!(active(&s, "permit/k1"));
    let r = s
        .revoke(&mut reg, &id("permit/k1"), now(), &ACTOR, "rotation test")
        .unwrap();
    assert_eq!(r.material, MaterialDigest::of(&m));
    assert!(matches!(
        s.key(KeyPurpose::Permit, &id("permit/k1"), now()),
        Ok(KeyStatus::Revoked)
    ));
    assert_eq!(
        s.revoke(&mut reg, &id("permit/zz"), now(), &ACTOR, "x")
            .err(),
        Some(RevokeError::Unknown)
    );
    // Reload: the revoked id is refused.
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    assert_eq!(refusal_of(&s, "permit_k1.json"), Some(Refused::Revoked));
    // The same material under a new id (and another purpose) is refused too.
    fs::remove_file(f.dir.join("permit_k1.json")).unwrap();
    f.key("permit/k2", &m);
    f.key("ingress/k3", &m);
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    assert_eq!(refusal_of(&s, "permit_k2.json"), Some(Refused::Revoked));
    assert_eq!(refusal_of(&s, "ingress_k3.json"), Some(Refused::Revoked));
    assert_eq!(s.loaded().count(), 0);
}

#[test]
fn ids_are_never_reused_and_material_is_never_shared() {
    let f = Fixture::new("reuse");
    f.key("permit/k1", &canary(1));
    let mut reg = MemoryKeyRegistry::new();
    assert!(active(
        &FileKeySource::load(&f.config(), &mut reg).unwrap(),
        "permit/k1"
    ));
    // The same id with new material.
    f.key("permit/k1", &canary(9));
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    assert_eq!(refusal_of(&s, "permit_k1.json"), Some(Refused::IdConflict));
    // Two ids with the same material (also across purposes).
    let f2 = Fixture::new("shared");
    let m = canary(3);
    f2.key("ingress/a", &m);
    f2.key("permit/a", &m);
    let s = f2.load_ok();
    assert_eq!(s.loaded().count(), 1);
    assert_eq!(s.diagnostics().len(), 1);
    assert_eq!(s.diagnostics()[0].refused, Refused::MaterialConflict);
}

#[test]
fn unavailable_registry_fails_the_load() {
    let f = Fixture::new("registry");
    f.key("permit/k1", &canary(1));
    let mut reg = MemoryKeyRegistry::new();
    reg.set_unavailable(true);
    assert_eq!(
        FileKeySource::load(&f.config(), &mut reg).err(),
        Some(DirRefusal::Registry)
    );
    let s = {
        let mut ok = MemoryKeyRegistry::new();
        FileKeySource::load(&f.config(), &mut ok).unwrap()
    };
    assert_eq!(
        s.revoke(&mut reg, &id("permit/k1"), now(), &ACTOR, "x")
            .err(),
        Some(RevokeError::Unavailable)
    );
    assert!(
        active(&s, "permit/k1"),
        "nothing changed when the ledger refused"
    );
}

#[test]
fn key_directory_checks() {
    // Mode other than 0700.
    let f = Fixture::new("dirmode");
    for mode in [0o750, 0o755, 0o711, 0o500] {
        fs::set_permissions(&f.dir, fs::Permissions::from_mode(mode)).unwrap();
        assert_eq!(f.load().0.err(), Some(DirRefusal::Mode(mode)), "{mode:o}");
    }
    fs::set_permissions(&f.dir, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(f.load().0.is_ok());
    // Not owned by the configured service uid.
    let mut other = f.config();
    other.owner_uid = f.uid.wrapping_add(1);
    other.ancestor_base = Some(f.dir.clone());
    assert_eq!(
        FileKeySource::load(&other, &mut MemoryKeyRegistry::new()).err(),
        Some(DirRefusal::Owner)
    );
    // Missing directory.
    let mut missing = f.config();
    missing.dir = f.base.join("absent");
    assert!(matches!(
        FileKeySource::load(&missing, &mut MemoryKeyRegistry::new()).err(),
        Some(DirRefusal::Open { errno, .. }) if errno == Errno::NOENT.raw_os_error()
    ));
    // Not normalized or relative.
    for dir in [
        "relative/keys",
        "/a/../keys",
        "/a/./keys",
        "/a//keys",
        "/a/keys/",
        "/",
    ] {
        let mut c = f.config();
        c.dir = PathBuf::from(dir);
        assert_eq!(
            FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).err(),
            Some(DirRefusal::PathNotNormalized),
            "{dir}"
        );
    }
}

#[test]
fn symlinked_key_directory_or_ancestor_is_refused() {
    let f = Fixture::new("dirlink");
    let real = f.base.join("real");
    mkdir(&real, 0o700);
    let link = f.base.join("link");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let mut c = f.config();
    c.dir = link.clone();
    // A trailing symlink opened with O_DIRECTORY | O_NOFOLLOW fails with ENOTDIR
    // (Linux checks the type of the unfollowed link); either way it is never followed.
    let r = FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).err();
    assert!(
        matches!(r, Some(DirRefusal::Open { errno, .. }) if errno == Errno::NOTDIR.raw_os_error()),
        "{r:?}"
    );
    // A symlinked ancestor (the walk opens one component per call, so it is refused the same way).
    let inner = real.join("keys");
    mkdir(&inner, 0o700);
    c.dir = link.join("keys");
    assert!(matches!(
        FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).err(),
        Some(DirRefusal::Open { errno, .. }) if errno == Errno::NOTDIR.raw_os_error()
    ));
    c.dir = inner;
    assert!(FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).is_ok());
}

#[test]
fn writable_ancestor_is_refused() {
    let f = Fixture::new("ancestor");
    let mid = f.base.join("mid");
    mkdir(&mid, 0o770);
    let dir = mid.join("keys");
    mkdir(&dir, 0o700);
    let mut c = f.config();
    c.dir = dir;
    assert_eq!(
        FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).err(),
        Some(DirRefusal::Ancestor {
            path: mid.clone(),
            reason: "group- or other-writable"
        })
    );
    fs::set_permissions(&mid, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).is_ok());
}

#[test]
fn the_full_walk_refuses_a_world_writable_ancestor() {
    // Without the test base, every ancestor from `/` is checked. The process
    // temporary directory is normally world-writable (`/tmp`, 1777), or owned
    // by the test user rather than root or the configured uid.
    let f = Fixture::new("fullwalk");
    let mut c = f.config();
    c.ancestor_base = None;
    let unsafe_ancestor = f
        .dir
        .ancestors()
        .skip(1)
        .filter(|a| {
            let m = fs::metadata(a).unwrap();
            m.mode() & 0o022 != 0 || (m.uid() != 0 && m.uid() != f.uid)
        })
        .last();
    let result = FileKeySource::load(&c, &mut MemoryKeyRegistry::new());
    match unsafe_ancestor {
        Some(path) => assert_eq!(
            result.err().map(|r| match r {
                DirRefusal::Ancestor { path, .. } => path,
                other => panic!("{other:?}"),
            }),
            Some(path.to_path_buf()),
            "the first unsafe ancestor from / is reported"
        ),
        None => assert!(result.is_ok()),
    }
}

/// A POSIX ACL in the kernel's xattr format: version 2, then entries (tag,
/// perm, id), sorted by tag. Mask 0 keeps the group mode bits at 0.
fn acl_blob(named_uid: Option<u32>) -> Vec<u8> {
    let mut v = 2u32.to_le_bytes().to_vec();
    let mut entry = |tag: u16, perm: u16, id: u32| {
        v.extend_from_slice(&tag.to_le_bytes());
        v.extend_from_slice(&perm.to_le_bytes());
        v.extend_from_slice(&id.to_le_bytes());
    };
    entry(0x01, 6, u32::MAX);
    if let Some(uid) = named_uid {
        entry(0x02, 4, uid);
    }
    entry(0x04, 0, u32::MAX);
    if named_uid.is_some() {
        entry(0x10, 0, u32::MAX);
    }
    entry(0x20, 0, u32::MAX);
    v
}

#[test]
fn acls_are_refused() {
    let f = Fixture::new("acl");
    let p = f.key("permit/k1", &canary(1));
    let fd = rustix::fs::open(&p, OFlags::RDONLY, Mode::empty()).unwrap();
    fsetxattr(
        &fd,
        "system.posix_acl_access",
        &acl_blob(Some(f.uid.wrapping_add(1))),
        XattrFlags::empty(),
    )
    .expect("this filesystem must support POSIX ACLs for this test");
    assert_eq!(fs::metadata(&p).unwrap().mode() & 0o7777, 0o600);
    let s = f.load_ok();
    assert_eq!(refusal_of(&s, "permit_k1.json"), Some(Refused::Acl));
    // A default ACL on the key directory.
    let g = Fixture::new("dacl");
    let dfd = rustix::fs::open(&g.dir, OFlags::RDONLY | OFlags::DIRECTORY, Mode::empty()).unwrap();
    fsetxattr(
        &dfd,
        "system.posix_acl_default",
        &acl_blob(None),
        XattrFlags::empty(),
    )
    .unwrap();
    assert_eq!(g.load().0.err(), Some(DirRefusal::Acl));
}

#[test]
fn file_checks_on_observed_facts() {
    // Ownership cannot be varied without privilege; the check is a pure function.
    let ok = Observed {
        file_type: FileType::RegularFile,
        uid: 990,
        mode: 0o600,
        nlink: 1,
        size: 300,
    };
    assert_eq!(check_file(&ok, 990), Ok(()));
    assert_eq!(check_file(&ok, 991), Err(Refused::Owner));
    assert_eq!(
        check_file(&Observed { size: 4097, ..ok }, 990),
        Err(Refused::TooLarge)
    );
    assert_eq!(
        check_file(&Observed { size: -1, ..ok }, 990),
        Err(Refused::TooLarge)
    );
    assert_eq!(
        check_file(
            &Observed {
                file_type: FileType::Socket,
                ..ok
            },
            990
        ),
        Err(Refused::NotRegular)
    );
    let dir = Observed {
        file_type: FileType::Directory,
        mode: 0o755,
        ..ok
    };
    assert_eq!(check_ancestor(&Observed { uid: 0, ..dir }, 990), Ok(()));
    assert_eq!(check_ancestor(&dir, 990), Ok(()));
    assert!(check_ancestor(&Observed { uid: 1000, ..dir }, 990).is_err());
    assert!(check_ancestor(
        &Observed {
            mode: 0o1777,
            uid: 0,
            ..dir
        },
        990
    )
    .is_err());
    assert_eq!(check_key_dir(&Observed { mode: 0o700, ..dir }, 990), Ok(()));
    assert_eq!(
        check_key_dir(
            &Observed {
                mode: 0o700,
                uid: 0,
                ..dir
            },
            990
        ),
        Err(DirRefusal::Owner)
    );
}

#[test]
fn rotation_overlap_and_issuing_selection() {
    let f = Fixture::new("rotation");
    // The old key ends at 00:10; the new one starts at 00:05 (overlap 5 min).
    f.key_window(
        "permit/2026-11-a",
        "2026-11-01T00:00:00Z",
        "2026-11-15T00:10:00Z",
        &canary(1),
    );
    f.key_window(
        "permit/2026-11-b",
        "2026-11-15T00:05:00Z",
        "2026-12-15T00:00:00Z",
        &canary(2),
    );
    f.key_window(
        "permit/2026-12-a",
        "2026-12-10T00:00:00Z",
        "2027-01-10T00:00:00Z",
        &canary(3),
    );
    let s = f.load_ok();
    let ttl = Duration::from_secs(30);
    let pick = |at: &str| {
        s.issuing_key(KeyPurpose::Permit, t(at), ttl)
            .map(|(k, _)| k.as_str().to_string())
    };
    // Before the new key is valid, the old one issues.
    assert_eq!(
        pick("2026-11-15T00:00:00Z").as_deref(),
        Ok("permit/2026-11-a")
    );
    // Still the old key: its not_after (00:10) is beyond now + 30 s + 60 s (00:05:30).
    assert_eq!(
        pick("2026-11-15T00:04:00Z").as_deref(),
        Ok("permit/2026-11-a")
    );
    // Once valid, the new key (latest not_before) issues...
    assert_eq!(
        pick("2026-11-15T00:05:00Z").as_deref(),
        Ok("permit/2026-11-b")
    );
    // ...while the old one still verifies until its not_after.
    let old = id("permit/2026-11-a");
    assert!(matches!(
        s.key(KeyPurpose::Permit, &old, t("2026-11-15T00:09:59Z")),
        Ok(KeyStatus::Active(_))
    ));
    assert!(matches!(
        s.key(KeyPurpose::Permit, &old, t("2026-11-15T00:10:00Z")),
        Ok(KeyStatus::Expired)
    ));
    // The next key takes over when it becomes valid.
    assert_eq!(
        pick("2026-12-10T00:00:00Z").as_deref(),
        Ok("permit/2026-12-a")
    );
    // A revoked key is never selected.
    let mut reg = MemoryKeyRegistry::new();
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    s.revoke(
        &mut reg,
        &id("permit/2026-11-b"),
        t("2026-11-16T00:00:00Z"),
        &ACTOR,
        "x",
    )
    .unwrap();
    // With the new key revoked, the old one issues until it is near expiry:
    // at 00:08:30 its not_after equals now + 30 s + 60 s, one second later it does not.
    assert_eq!(
        s.issuing_key(KeyPurpose::Permit, t("2026-11-15T00:08:30Z"), ttl)
            .map(|(k, _)| k),
        Ok(id("permit/2026-11-a"))
    );
    assert!(s
        .issuing_key(KeyPurpose::Permit, t("2026-11-15T00:08:31Z"), ttl)
        .is_err());
    assert!(s
        .issuing_key(KeyPurpose::Permit, t("2026-11-20T00:00:00Z"), ttl)
        .is_err());
    assert_eq!(
        s.issuing_key(KeyPurpose::Permit, t("2026-12-11T00:00:00Z"), ttl)
            .map(|(k, _)| k),
        Ok(id("permit/2026-12-a"))
    );
}

#[test]
fn entry_bound_makes_the_source_unavailable() {
    let f = Fixture::new("bound");
    for i in 0..3u8 {
        f.key(&format!("permit/k{i}"), &canary(i));
    }
    let mut c = f.config();
    c.max_entries = 2;
    assert_eq!(
        FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).err(),
        Some(DirRefusal::TooManyEntries)
    );
    c.max_entries = 3;
    assert!(FileKeySource::load(&c, &mut MemoryKeyRegistry::new()).is_ok());
}

#[test]
fn material_never_appears_in_debug_or_diagnostics() {
    let f = Fixture::new("canary");
    let good = canary(0x5a);
    let bad = canary(0xa5);
    f.key("permit/good", &good);
    // Refused files carrying material: wrong mode, wrong name, bad window.
    let p = f.key("permit/loose", &bad);
    fs::set_permissions(p, fs::Permissions::from_mode(0o644)).unwrap();
    f.write_raw(
        "permit_named.json",
        &Fixture::doc("permit", "permit/other", NB, NA, &bad),
        0o600,
    );
    f.write_raw(
        "permit_window.json",
        &Fixture::doc("permit", "permit/window", NA, NB, &bad),
        0o600,
    );
    let mut reg = MemoryKeyRegistry::new();
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    assert_eq!(s.diagnostics().len(), 3);
    let k = id("permit/good");
    let status = s.key(KeyPurpose::Permit, &k, now()).unwrap();
    let issued = s.issuing_key(KeyPurpose::Permit, now(), Duration::from_secs(30));
    let mut out = format!("{s:?}\n{status:?}\n{issued:?}\n{reg:?}\n{:?}\n", f.config());
    for d in s.diagnostics() {
        out.push_str(&format!("{d}\n{d:?}\n"));
    }
    for m in [&good, &bad] {
        let h = hex(m);
        assert!(!out.contains(&h), "material hex leaked");
        assert!(!out.contains(&h[..16]), "material hex prefix leaked");
        assert!(
            !out.contains(&format!("{:?}", &m[..8])),
            "material bytes leaked"
        );
    }
    assert!(out.contains("redacted"));
}

/// M5a-13 L9: a revocation recorded in the ledger by another path (`admin.sock`) reaches
/// the loaded source, so issuing falls back to a valid older key instead of selecting the
/// revoked one (which `issuing_key_at_use` would refuse).
#[test]
fn ledger_revocations_reach_the_source_and_issuing_falls_back() {
    use writ_broker::keys::{issuing_key_at_use, KeyRegistry, KeyRevocation, KeyRevocations};
    let f = Fixture::new("sync");
    let (a, b) = (canary(1), canary(2));
    f.key_window(
        "permit/old",
        "2026-01-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        &a,
    );
    f.key_window(
        "permit/new",
        "2026-06-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        &b,
    );
    let mut reg = MemoryKeyRegistry::new();
    let s = FileKeySource::load(&f.config(), &mut reg).unwrap();
    let at = t("2026-11-16T00:00:00Z");
    let ttl = Duration::from_secs(30);
    assert_eq!(
        s.issuing_key(KeyPurpose::Permit, at, ttl).map(|(k, _)| k),
        Ok(id("permit/new"))
    );
    // The ledger records the revocation without this source (as admin.sock does).
    reg.revoke(KeyRevocation {
        key_id: id("permit/new"),
        material: MaterialDigest::of(&b),
        purpose: KeyPurpose::Permit,
        revoked_at: at,
        actor: ACTOR.document().unwrap(),
        reason: "compromised".into(),
    })
    .unwrap();
    assert!(reg
        .is_revoked(&id("permit/new"), &MaterialDigest::of(&b))
        .unwrap());
    // Before the sync: the revoked key is still selected and issuing refuses.
    assert!(issuing_key_at_use(&s, &reg, KeyPurpose::Permit, at, ttl).is_err());
    assert_eq!(s.sync_revocations(&reg), Ok(1));
    assert_eq!(s.sync_revocations(&reg), Ok(0));
    assert_eq!(
        issuing_key_at_use(&s, &reg, KeyPurpose::Permit, at, ttl).map(|(k, _)| k),
        Ok(id("permit/old"))
    );
    assert!(matches!(
        s.key(KeyPurpose::Permit, &id("permit/new"), at),
        Ok(KeyStatus::Revoked)
    ));
    // An unavailable ledger marks nothing.
    reg.set_unavailable(true);
    assert!(s.sync_revocations(&reg).is_err());
}

/// M5a-13 L9: the J-3 issuing rule for one named key (seals).
#[test]
fn a_named_key_issues_only_within_its_window_and_margin() {
    let f = Fixture::new("named");
    f.key_window(
        "ingress/k1",
        "2026-01-01T00:00:00Z",
        "2026-11-16T00:02:00Z",
        &canary(4),
    );
    let s = f.load_ok();
    let ttl = Duration::from_secs(30);
    let k = id("ingress/k1");
    // not_after must be at least now + 30 s + 60 s.
    assert!(s
        .issuing_named_key(KeyPurpose::Ingress, &k, t("2026-11-16T00:00:30Z"), ttl)
        .is_ok());
    assert!(s
        .issuing_named_key(KeyPurpose::Ingress, &k, t("2026-11-16T00:00:31Z"), ttl)
        .is_err());
    assert!(s
        .issuing_named_key(KeyPurpose::Permit, &k, t("2026-11-16T00:00:00Z"), ttl)
        .is_err());
    assert!(s
        .issuing_named_key(
            KeyPurpose::Ingress,
            &id("ingress/k2"),
            t("2026-11-16T00:00:00Z"),
            ttl
        )
        .is_err());
}
