//! M5a-04 service tests over real Unix sockets in user space (J-1, J-2, J-3, J-4, J-11.8).
//!
//! Every test runs as the current unprivileged user: agent, admin and other identities are
//! simulated by configuring the current uid into (or out of) the admission tables; real
//! cross-uid denial (agent uid against `admin.sock`, the trusted paths and keys) is verified
//! on the reference VM (P-AGENT-DENY, P-PEERCRED), not here.
//!
//! The key directory check walks every ancestor (root- or service-owned, not group- or
//! other-writable), so the test state lives under the first candidate base whose ancestors
//! pass (`$XDG_RUNTIME_DIR`, then `$HOME`); none qualifying fails the test (no silent skip).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use writ_broker::writ_kernel::json::{self, Value};
use writ_rpc::frame::{read_frame, write_control, write_frame, Frame, FrameLimits, FrameType};
use writ_service::config::ServiceConfig;
use writ_service::{Running, Service, StartError};

const INGRESS_HEX: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const PERMIT_HEX: &str = "4444444444444444444444444444444444444444444444444444444444444444";
const OTHER_UID: u32 = 4_000_001;

fn me() -> u32 {
    std::fs::metadata("/proc/self").unwrap().uid()
}

fn my_gid() -> u32 {
    std::fs::metadata("/proc/self").unwrap().gid()
}

fn ancestors_pass(dir: &Path, uid: u32) -> bool {
    let mut p = PathBuf::from("/");
    let mut ok = true;
    for c in dir.components().skip(1) {
        let m = match std::fs::symlink_metadata(&p) {
            Ok(m) => m,
            Err(_) => return false,
        };
        ok &= m.is_dir() && (m.uid() == 0 || m.uid() == uid) && m.mode() & 0o022 == 0;
        p.push(c);
    }
    let m = std::fs::symlink_metadata(&p);
    ok && m.is_ok_and(|m| m.is_dir() && m.uid() == uid && m.mode() & 0o022 == 0)
}

fn base() -> PathBuf {
    let candidates = ["XDG_RUNTIME_DIR", "HOME"]
        .into_iter()
        .filter_map(|v| std::env::var_os(v).map(PathBuf::from))
        .chain([PathBuf::from(env!("CARGO_TARGET_TMPDIR"))]);
    for d in candidates {
        if d.is_absolute() && ancestors_pass(&d, me()) {
            return d;
        }
    }
    panic!("no base directory whose ancestors pass the key-directory check");
}

struct Env {
    dir: PathBuf,
}

impl Env {
    fn new(name: &str) -> Env {
        let dir = base().join(format!("writ-svc-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let keys = dir.join("keys");
        std::fs::create_dir(&keys).unwrap();
        std::fs::set_permissions(&keys, std::fs::Permissions::from_mode(0o700)).unwrap();
        let env = Env { dir };
        env.key("ingress", "k1", INGRESS_HEX);
        env.key("permit", "k1", PERMIT_HEX);
        env
    }

    fn key(&self, purpose: &str, label: &str, hex: &str) {
        let path = self
            .dir
            .join("keys")
            .join(format!("{purpose}_{label}.json"));
        let body = format!(
            r#"{{"contract":"writ-key/1","purpose":"{purpose}","key_id":"{purpose}/{label}","not_before":"2026-01-01T00:00:00Z","not_after":"2030-01-01T00:00:00Z","material":"{hex}"}}"#
        );
        std::fs::write(&path, body).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn host(&self) -> PathBuf {
        self.dir.join("host.sock")
    }

    fn admin(&self) -> PathBuf {
        self.dir.join("admin.sock")
    }

    /// A configuration where `agent` is the agent uid bound on host.sock and `admin` the
    /// admin uid. The service uid is this uid (it owns the key directory). When `agent` is
    /// this uid too, the validated configuration is built with a placeholder agent uid and
    /// then rebound to this uid: on one workstation account the agent and the service cannot
    /// be different uids, and `ServiceConfig::parse` (which the binary uses) refuses an agent
    /// uid equal to the service uid. `Service::open` trusts its caller's configuration.
    fn config(&self, agent: u32, admin: u32, per_uid: usize) -> ServiceConfig {
        let mut c = self.parsed(
            if agent == me() { OTHER_UID } else { agent },
            admin,
            per_uid,
        );
        if agent == me() {
            c.agent_uids = vec![me()];
            c.host_bindings[0].uid = me();
        }
        c
    }

    fn parsed(&self, agent: u32, admin: u32, per_uid: usize) -> ServiceConfig {
        let d = |p: &str| self.dir.join(p).to_str().unwrap().to_string();
        let doc = format!(
            r#"{{"contract":"writ-service-config/1",
            "service":{{"uid":{svc},"gid":{gid}}},
            "identities":{{"agent_uids":[{agent}],"desktop_uids":[4000100],"job_uid":4000200}},
            "ledger":"{ledger}","snapshots":"{snaps}",
            "keys":{{"dir":"{keys}"}},
            "sockets":{{"host":"{host}","admin":"{adm}"}},
            "host_bindings":[{{"uid":{agent},"tenant_id":"acme","project_id":"web","principal_id":"agent:writ-agent","roles":["agent"],"adapter_id":"writ-host-adapter:cli","ingress_key_id":"ingress/k1"}}],
            "admin_uids":[{admin}],
            "limits":{{"max_connections":8,"max_connections_per_uid":{per_uid},"idle_seconds":2,"request_seconds":2}}}}"#,
            svc = me(),
            gid = my_gid(),
            ledger = d("ledger.db"),
            snaps = d("snapshots.db"),
            keys = d("keys"),
            host = d("host.sock"),
            adm = d("admin.sock"),
        );
        ServiceConfig::parse(doc.as_bytes()).unwrap()
    }

    fn start(&self, config: ServiceConfig) -> Running {
        if !self.dir.join("ledger.db").exists() {
            Service::initialize(&config).unwrap();
        }
        Service::open(config).unwrap().serve().unwrap()
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn connect(path: &Path) -> UnixStream {
    let s = UnixStream::connect(path).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s
}

fn call(s: &mut UnixStream, v: &Value) -> Value {
    write_control(s, v).unwrap();
    reply(s)
}

fn reply(s: &mut UnixStream) -> Value {
    match read_frame(s, &FrameLimits::host()).unwrap() {
        Frame::Control(v) => v,
        Frame::Data(_) => panic!("data reply"),
    }
}

fn ok(v: &Value) -> bool {
    v.as_obj().unwrap().get("ok") == Some(&Value::Bool(true))
}

fn error(v: &Value) -> String {
    v.as_obj().unwrap()["error"].as_str().unwrap().to_string()
}

fn op(name: &str) -> Value {
    json::obj([("op", json::s(name))])
}

/// The server closed the connection without answering anything.
fn closed_unanswered(s: &mut UnixStream) -> bool {
    let mut buf = [0u8; 16];
    match s.read(&mut buf) {
        Ok(0) => true,
        Err(e) => e.kind() == std::io::ErrorKind::ConnectionReset,
        Ok(_) => false,
    }
}

#[test]
fn an_admitted_agent_ingests_a_snapshot_and_admin_sock_refuses_it_unread() {
    let env = Env::new("ingest");
    let run = env.start(env.config(me(), 0, 4));
    // admin.sock admits uid 0 only here: this uid is closed without a reply.
    let mut a = connect(&env.admin());
    let _ = write_control(&mut a, &op("readiness"));
    assert!(closed_unanswered(&mut a));
    let mut h = connect(&env.host());
    assert!(ok(&call(&mut h, &op("snapshot.ingest.begin"))));
    let file = json::obj([
        ("op", json::s("snapshot.ingest.file")),
        ("path", json::s("src/a.txt")),
        ("size", Value::Int(5)),
    ]);
    assert!(ok(&call(&mut h, &file)));
    write_frame(&mut h, FrameType::Data, b"he").unwrap();
    write_frame(&mut h, FrameType::Data, b"llo").unwrap();
    let c = call(&mut h, &op("snapshot.ingest.commit"));
    assert!(ok(&c), "{c:?}");
    let id = c.as_obj().unwrap()["snapshot_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(id.starts_with("snap-"));
    // Scope came from the binding: the store has it under acme/web.
    let svc = run.service().clone();
    let read = svc.snapshots().read("acme", "web", &id).unwrap();
    assert!(read.is_some());
    // status.receipt for an unknown client key: null (scoped to this uid and binding).
    let r = call(
        &mut h,
        &json::obj([
            ("op", json::s("status.receipt")),
            ("idempotency_key", json::s("k1")),
        ]),
    );
    assert_eq!(r.as_obj().unwrap()["status"], Value::Null);
    drop(h);
    drop(svc);
    run.shutdown().unwrap();
}

#[test]
fn unlisted_uids_are_refused_on_host_sock_and_admin_answers_readiness_without_material() {
    let env = Env::new("admin");
    let run = env.start(env.config(OTHER_UID, me(), 4));
    let mut h = connect(&env.host());
    let _ = write_control(&mut h, &op("snapshot.ingest.begin"));
    assert!(closed_unanswered(&mut h), "unlisted uid on host.sock");
    let mut a = connect(&env.admin());
    let r = call(&mut a, &op("readiness"));
    assert!(ok(&r));
    let m = r.as_obj().unwrap();
    assert_eq!(m["label"], json::s("unverified"));
    assert_eq!(m["deployment_verified"], Value::Bool(false));
    assert_eq!(m["snapshot_store_immutable"], Value::Bool(false));
    let ids: Vec<String> = match &m["keys"] {
        Value::Arr(ks) => ks
            .iter()
            .map(|k| k.as_obj().unwrap()["key_id"].as_str().unwrap().to_string())
            .collect(),
        _ => panic!(),
    };
    assert_eq!(ids, vec!["ingress/k1".to_string(), "permit/k1".to_string()]);
    // Canary: no material anywhere in the answer.
    let text = json::canonical(&r).unwrap();
    assert!(!text.contains(&INGRESS_HEX[..16]) && !text.contains(&PERMIT_HEX[..16]));
    drop(a);
    run.shutdown().unwrap();
}

#[test]
fn transport_violations_answer_the_generic_error_and_close() {
    let env = Env::new("frames");
    let run = env.start(env.config(me(), 0, 8));
    let generic = |bytes: &[u8]| {
        let mut h = connect(&env.host());
        h.write_all(bytes).unwrap();
        let r = reply(&mut h);
        assert_eq!(error(&r), "REQUEST_REFUSED", "{bytes:?}");
        assert!(closed_unanswered(&mut h));
    };
    // Oversized control length (refused before allocation), unknown type, bad JSON,
    // duplicate members, non-object, unknown op, data frame without an ingest file.
    generic(&[0x00, 0x01, 0x00, 0x01, 0x01]);
    generic(&[0xff, 0xff, 0xff, 0xff, 0x02]);
    generic(&[0, 0, 0, 2, 0x09, b'{', b'}']);
    generic(&[0, 0, 0, 3, 0x01, b'{', b'x', b'}']);
    let dup = br#"{"op":"status.receipt","op":"x"}"#;
    let mut f = (dup.len() as u32).to_be_bytes().to_vec();
    f.push(1);
    f.extend_from_slice(dup);
    generic(&f);
    generic(&[0, 0, 0, 2, 0x01, b'[', b']']);
    let mut h = connect(&env.host());
    assert_eq!(error(&call(&mut h, &op("keys.revoke"))), "REQUEST_REFUSED");
    let mut h = connect(&env.host());
    write_frame(&mut h, FrameType::Data, b"x").unwrap();
    assert_eq!(error(&reply(&mut h)), "REQUEST_REFUSED");
    // A message carrying a descriptor (SCM_RIGHTS) closes the connection.
    {
        use std::os::fd::AsFd;
        let mut h = connect(&env.host());
        let (passed, _keep) = UnixStream::pair().unwrap();
        let frame =
            writ_rpc::frame::encode(FrameType::Control, br#"{"op":"snapshot.ingest.begin"}"#)
                .unwrap();
        writ_hostsys::sock::send_with_descriptor(&h, &frame, passed.as_fd()).unwrap();
        assert_eq!(error(&reply(&mut h)), "REQUEST_REFUSED");
        assert!(closed_unanswered(&mut h));
    }
    run.shutdown().unwrap();
}

#[test]
fn the_per_uid_cap_closes_extra_connections_unread() {
    let env = Env::new("caps");
    let run = env.start(env.config(me(), 0, 1));
    let mut first = connect(&env.host());
    assert!(ok(&call(&mut first, &op("snapshot.ingest.begin"))));
    let mut second = connect(&env.host());
    let _ = write_control(&mut second, &op("snapshot.ingest.begin"));
    assert!(closed_unanswered(&mut second), "over the per-uid cap");
    drop(first);
    // The slot is released when the first connection ends.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let mut again = connect(&env.host());
        if write_control(&mut again, &op("snapshot.ingest.begin")).is_ok() {
            if let Ok(Frame::Control(v)) = read_frame(&mut again, &FrameLimits::host()) {
                assert!(ok(&v));
                break;
            }
        }
        assert!(Instant::now() < deadline, "slot never released");
        std::thread::sleep(Duration::from_millis(50));
    }
    run.shutdown().unwrap();
}

/// M5a-13 L10: a peer that drips a frame one byte at a time (each byte within the idle
/// deadline) is closed at the per-request deadline, which frees its cap slot.
#[test]
fn a_slow_drip_peer_cannot_hold_its_cap_slot() {
    use std::io::Write as _;
    let env = Env::new("drip");
    let run = env.start(env.config(me(), 0, 1));
    let frame = writ_rpc::frame::encode(
        writ_rpc::FrameType::Control,
        br#"{"op":"snapshot.ingest.begin"}"#,
    )
    .unwrap();
    let mut slow = connect(&env.host());
    let started = Instant::now();
    let mut closed_after = None;
    for byte in &frame {
        if slow.write_all(&[*byte]).is_err() {
            closed_after = Some(started.elapsed());
            break;
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    // request_seconds is 2: the server closed before the drip (about 14 s) completed.
    let elapsed = closed_after.unwrap_or_else(|| started.elapsed());
    assert!(
        closed_after.is_some() || closed_unanswered(&mut slow),
        "the slow peer was served"
    );
    assert!(elapsed < Duration::from_secs(6), "{elapsed:?}");
    drop(slow);
    // The slot is free again.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let mut again = connect(&env.host());
        if write_control(&mut again, &op("snapshot.ingest.begin")).is_ok() {
            if let Ok(Frame::Control(v)) = read_frame(&mut again, &FrameLimits::host()) {
                assert!(ok(&v));
                break;
            }
        }
        assert!(Instant::now() < deadline, "slot never released");
        std::thread::sleep(Duration::from_millis(50));
    }
    run.shutdown().unwrap();
}

#[test]
fn keys_revoke_over_admin_sock_records_os_peer_provenance_and_refuses_the_key_at_next_load() {
    let env = Env::new("revoke");
    let config = env.config(OTHER_UID, me(), 4);
    let run = env.start(config.clone());
    let mut a = connect(&env.admin());
    let scope = json::obj([
        ("kind", json::s("key_id")),
        ("purpose", json::s("ingress")),
        ("key_id", json::s("ingress/k1")),
    ]);
    let revoke = |a: &mut UnixStream, scope: &Value, reason: &str| {
        call(
            a,
            &json::obj([
                ("op", json::s("keys.revoke")),
                ("scope", scope.clone()),
                ("reason", json::s(reason)),
            ]),
        )
    };
    let r = revoke(&mut a, &scope, "rotation \"test\"\n<b>");
    assert!(ok(&r), "{r:?}");
    let rev = r.as_obj().unwrap()["revocation"].as_obj().unwrap().clone();
    assert_eq!(rev["created"], Value::Bool(true));
    assert_eq!(rev["key_id"], json::s("ingress/k1"));
    let again = revoke(&mut a, &scope, "again");
    assert_eq!(
        again.as_obj().unwrap()["revocation"].as_obj().unwrap()["created"],
        Value::Bool(false)
    );
    // Purpose mismatch, unregistered id, wildcard-like and malformed scopes refuse.
    let bad_purpose = json::obj([
        ("kind", json::s("material")),
        ("purpose", json::s("permit")),
        ("material_digest", rev["material_digest"].clone()),
    ]);
    assert_eq!(
        error(&revoke(&mut a, &bad_purpose, "x")),
        "KEY_PURPOSE_MISMATCH"
    );
    let unknown = json::obj([
        ("kind", json::s("key_id")),
        ("purpose", json::s("ingress")),
        ("key_id", json::s("ingress/none")),
    ]);
    assert_eq!(error(&revoke(&mut a, &unknown, "x")), "KEY_UNREGISTERED");
    let all = json::obj([("kind", json::s("all")), ("purpose", json::s("ingress"))]);
    assert_eq!(error(&revoke(&mut a, &all, "x")), "KEYS_REVOKE_INVALID");
    // The audit row carries the OS peer, endpoint and pid.
    let svc = run.service().clone();
    let rows = svc.ledger().key_revocations().unwrap();
    assert_eq!(rows.len(), 1);
    assert!(rows[0]
        .actor
        .contains(&format!("\"os_principal\":\"uid:{}\"", me())));
    assert!(rows[0].actor.contains("\"endpoint\":\"admin.sock\""));
    assert!(rows[0]
        .actor
        .contains(&format!("\"pid\":{}", std::process::id())));
    drop(a);
    drop(svc);
    run.shutdown().unwrap();
    // At the next start the revoked key is refused at load, the other is kept.
    let svc = Service::open(config).unwrap();
    let loaded: Vec<String> = svc
        .keys()
        .loaded()
        .map(|(k, _)| k.as_str().to_string())
        .collect();
    assert_eq!(loaded, vec!["permit/k1".to_string()]);
    assert!(svc
        .keys()
        .diagnostics()
        .iter()
        .any(|d| d.entry == "ingress_k1.json" && format!("{:?}", d.refused) == "Revoked"));
}

#[test]
fn one_owner_at_a_time() {
    let env = Env::new("owner");
    let config = env.config(me(), 0, 4);
    let run = env.start(config.clone());
    match Service::open(config.clone()) {
        Err(StartError::Ledger(e)) => assert_eq!(e, writ_broker::store::StoreError::OwnedElsewhere),
        Err(other) => panic!("{other}"),
        Ok(_) => panic!("second owner admitted"),
    }
    run.shutdown().unwrap();
    // After the owner is gone a new owner opens at once.
    let svc = Service::open(config).unwrap();
    drop(svc);
}

#[test]
fn the_binary_refuses_to_run_outside_the_exact_service_identity() {
    let env = Env::new("binary");
    let config_path = env.dir.join("service.json");
    let d = |p: &str| env.dir.join(p).to_str().unwrap().to_string();
    let doc = format!(
        r#"{{"contract":"writ-service-config/1","service":{{"uid":{u},"gid":{g}}},
        "identities":{{"agent_uids":[4000001],"desktop_uids":[4000100],"job_uid":4000200}},
        "ledger":"{l}","snapshots":"{s}","keys":{{"dir":"{k}"}},
        "sockets":{{"host":"{h}","admin":"{a}"}},
        "host_bindings":[{{"uid":4000001,"tenant_id":"acme","project_id":"web","principal_id":"agent:a","roles":["agent"],"adapter_id":"ad","ingress_key_id":"ingress/k1"}}]}}"#,
        u = me(),
        g = my_gid(),
        l = d("ledger.db"),
        s = d("snapshots.db"),
        k = d("keys"),
        h = d("host.sock"),
        a = d("admin.sock"),
    );
    std::fs::write(&config_path, doc).unwrap();
    let bin = env!("CARGO_BIN_EXE_writ-serviced");
    let out = std::process::Command::new(bin)
        .args(["--config", config_path.to_str().unwrap()])
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    let groups = writ_hostsys::sock::process_identity().unwrap().groups;
    if groups.iter().any(|g| *g != my_gid()) {
        // This workstation user has supplementary groups: the J-1 startup check refuses.
        assert_eq!(out.status.code(), Some(3), "{stderr}");
        assert!(stderr.contains("SupplementaryGroups"), "{stderr}");
        assert!(!env.host().exists());
    } else {
        // Without supplementary groups the check passes and the ledger is absent.
        assert_eq!(out.status.code(), Some(1), "{stderr}");
    }
    // Unknown arguments and a missing configuration refuse before anything is opened.
    for args in [
        vec!["--config"],
        vec!["--key", "x", "--config", "y"],
        vec![],
    ] {
        let out = std::process::Command::new(bin)
            .args(&args)
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(2), "{args:?}");
    }
}
