#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use super::*;
use serde_json::{json, Value};
use std::future::Future;
use std::os::unix::fs::DirBuilderExt;
use std::path::PathBuf;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::pipe::{Receiver, Sender};
use tokio::net::UnixListener;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio::time::timeout;
use writ_rpc::{read_frame, Frame, FrameLimits, FrameType};

const LIMIT: Duration = Duration::from_secs(8);

struct Wire {
    input: Option<Sender>,
    output: BufReader<Receiver>,
    task: JoinHandle<Result<(), &'static str>>,
}

impl Wire {
    fn open(admission: Admission, handler: Handler) -> Self {
        let (server_read, client_write) = std::io::pipe().unwrap();
        let (client_read, server_write) = std::io::pipe().unwrap();
        let read = Receiver::from_owned_fd(server_read.into()).unwrap();
        let write = Sender::from_owned_fd(server_write.into()).unwrap();
        Self {
            input: Some(Sender::from_owned_fd(client_write.into()).unwrap()),
            output: BufReader::new(Receiver::from_owned_fd(client_read.into()).unwrap()),
            task: tokio::spawn(serve(read, write, admission, handler)),
        }
    }

    async fn send(&mut self, message: Value) {
        let mut line = message.to_string();
        line.push('\n');
        self.input
            .as_mut()
            .unwrap()
            .write_all(line.as_bytes())
            .await
            .unwrap();
    }

    async fn reply(&mut self, id: u64) -> Value {
        let mut line = String::new();
        assert_ne!(self.output.read_line(&mut line).await.unwrap(), 0, "EOF");
        let response: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(response["id"], id, "{response}");
        response
    }

    async fn initialize(&mut self, version: &str) {
        self.send(request(
            1,
            "initialize",
            json!({
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name":"wire-test", "version":"1"}
            }),
        ))
        .await;
        assert_eq!(
            self.reply(1).await["result"]["protocolVersion"],
            "2025-11-25"
        );
    }

    async fn initialized(&mut self) {
        self.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))
            .await;
    }

    async fn ping(&mut self, id: u64) {
        self.send(request(id, "ping", json!({}))).await;
        assert!(self.reply(id).await["result"].is_object());
    }

    async fn finish(mut self) {
        self.input.take();
        self.observe_eof().await;
    }

    async fn observe_eof(mut self) {
        let mut discarded = Vec::new();
        self.output.read_to_end(&mut discarded).await.unwrap();
        self.task.await.unwrap().unwrap();
    }
}

struct Peer {
    path: PathBuf,
    dir: PathBuf,
    listener: UnixListener,
}

impl Peer {
    fn bind() -> Self {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("writ-mcp-{}-{nonce}", std::process::id()));
        std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        let path = dir.join("host.sock");
        let listener = UnixListener::bind(&path).unwrap();
        Self {
            path,
            dir,
            listener,
        }
    }

    fn handler(&self, admission: Admission) -> Handler {
        Handler::recording(admission, self.path.clone(), uid())
    }

    async fn no_connect(&self) {
        // Check only after the shim has completed or replied to every tested request.
        if let Ok(accepted) = timeout(Duration::from_millis(50), self.listener.accept()).await {
            panic!("unexpected RPC connection: {accepted:?}");
        }
    }

    async fn frame(&self) -> (tokio::net::UnixStream, writ_kernel::json::Value) {
        let (mut stream, _) = self.listener.accept().await.unwrap();
        let mut header = [0; 5];
        stream.read_exact(&mut header).await.unwrap();
        let len = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
        assert!(len <= writ_rpc::frame::MAX_CONTROL_BYTES as usize);
        let mut bytes = header.to_vec();
        bytes.resize(5 + len, 0);
        stream.read_exact(&mut bytes[5..]).await.unwrap();
        let Frame::Control(frame) =
            read_frame(&mut bytes.as_slice(), &FrameLimits::admin()).unwrap()
        else {
            panic!("bad frame");
        };
        (stream, frame)
    }

    async fn zero_bytes(&self) {
        let (mut stream, _) = self.listener.accept().await.unwrap();
        assert_eq!(stream.read(&mut [0; 1]).await.unwrap(), 0);
    }
}

impl Drop for Peer {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
        let _ = std::fs::remove_dir(&self.dir);
    }
}

fn uid() -> u32 {
    writ_hostsys::sock::process_identity().unwrap().euid
}

fn request(id: u64, method: &str, params: Value) -> Value {
    json!({"jsonrpc":"2.0", "method":method, "id":id, "params":params})
}

fn args() -> Value {
    json!({
        "relative_path":"doc/result.txt",
        "source_path":"source.txt",
        "snapshot_id":format!("snap-{}", "a".repeat(64)),
        "manifest_digest":format!("sha256:{}", "b".repeat(64)),
        "content_digest":format!("sha256:{}", "c".repeat(64)),
        "idempotency_key":"wire-write-1",
    })
}

fn call(id: u64) -> Value {
    request(
        id,
        "tools/call",
        json!({"name":"fs.write", "arguments":args()}),
    )
}

fn refused(response: &Value) {
    assert!(
        response["error"].is_object() || response["result"]["isError"] == true,
        "expected refusal: {response}"
    );
}

fn run(future: impl Future<Output = ()>) {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async { timeout(LIMIT, future).await.unwrap() });
}

#[test]
fn pinned_handshake_and_only_valid_call_forwards_once() {
    run(async {
        let peer = Peer::bind();
        let admission = Admission::default();
        let mut wire = Wire::open(admission.clone(), peer.handler(admission));
        for (id, version) in [(2, "2025-11-25"), (3, "2026-07-28")] {
            let mut message = call(id);
            message["params"]["_meta"] = json!({"io.modelcontextprotocol/protocolVersion":version});
            wire.send(message).await;
            refused(&wire.reply(id).await);
        }
        wire.send(request(10, "tools/list", json!({}))).await;
        refused(&wire.reply(10).await);
        wire.initialize("2024-11-05").await;
        wire.send(request(11, "tools/list", json!({}))).await;
        refused(&wire.reply(11).await);
        wire.initialized().await;
        // No gap between initialized and tools/list is required.
        wire.send(request(4, "tools/list", json!({}))).await;
        assert_eq!(
            wire.reply(4).await["result"]["tools"][0]["name"],
            "fs.write"
        );
        wire.send(request(
            5,
            "tools/call",
            json!({"name":"shell", "arguments":args()}),
        ))
        .await;
        refused(&wire.reply(5).await);
        for (id, key, value) in [
            (6, "relative_path", json!("../wrong")),
            (7, "manifest_digest", json!("wrong")),
            (8, "tenant_id", json!("caller-scope")),
        ] {
            let mut message = call(id);
            message["params"]["arguments"][key] = value;
            wire.send(message).await;
            refused(&wire.reply(id).await);
        }
        let mut unsupported = call(12);
        unsupported["params"]["_meta"] =
            json!({"io.modelcontextprotocol/protocolVersion":"2026-07-28"});
        wire.send(unsupported).await;
        refused(&wire.reply(12).await);
        peer.no_connect().await;
        wire.send(call(9)).await;
        let (mut stream, frame) = peer.frame().await;
        let map = frame.as_obj().unwrap();
        assert_eq!(map.len(), 9);
        assert_eq!(map["op"].as_str(), Some("effect.request"));
        assert_eq!(map["action"].as_str(), Some("fs.write"));
        assert_eq!(map["mode"].as_str(), Some("create_only"));
        for (key, value) in args().as_object().unwrap() {
            assert_eq!(map[key].as_str(), value.as_str(), "{key}");
        }
        stream
            .write_all(
                &writ_rpc::frame::encode(
                    FrameType::Control,
                    br#"{"ok":true,"receipt_id":"receipt-wire"}"#,
                )
                .unwrap(),
            )
            .await
            .unwrap();
        let response = wire.reply(9).await;
        assert!(
            response.get("error").is_none() && response["result"]["isError"] != true,
            "{response}"
        );
        assert_eq!(
            response["result"]["structuredContent"]["receipt_id"],
            "receipt-wire"
        );
        assert_eq!(stream.read(&mut [0; 1]).await.unwrap(), 0);
        wire.finish().await;
        peer.no_connect().await;
    });
}

#[test]
fn delayed_callback_keeps_pre_initialized_refusal() {
    run(async {
        let peer = Peer::bind();
        let admission = Admission::default();
        let (entered, waiting) = oneshot::channel();
        let (resume, release) = oneshot::channel();
        let handler = peer
            .handler(admission.clone())
            .pause_before_call(entered, release);
        let mut wire = Wire::open(admission, handler);
        wire.initialize("2025-11-25").await;
        wire.send(call(2)).await;
        waiting.await.unwrap();
        wire.initialized().await;
        wire.ping(3).await;
        resume.send(()).unwrap();
        refused(&wire.reply(2).await);
        wire.finish().await;
        peer.no_connect().await;
    });
}

#[test]
fn cancel_disconnect_and_duplicates_revoke_pending_forward() {
    for case in [
        "cancel",
        "eof",
        "duplicate-id",
        "duplicate-init",
        "duplicate-initialized",
    ] {
        run(async {
            let peer = Peer::bind();
            let admission = Admission::default();
            let (entered, waiting) = oneshot::channel();
            let (resume, release) = oneshot::channel();
            let handler = peer
                .handler(admission.clone())
                .pause_before_submit(entered, release);
            let mut wire = Wire::open(admission.clone(), handler);
            wire.initialize("2025-11-25").await;
            wire.initialized().await;
            wire.send(call(2)).await;
            waiting.await.unwrap();
            let id = rmcp::model::RequestId::Number(2);
            assert!(admission.allowed(&id), "{case}");
            match case {
                "cancel" => {
                    wire.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled",
                        "params":{"requestId":2,"reason":"test"}}))
                        .await;
                    wire.ping(3).await;
                }
                "eof" => {
                    wire.input.take();
                }
                "duplicate-id" => wire.send(call(2)).await,
                "duplicate-init" => {
                    wire.send(request(
                        3,
                        "initialize",
                        json!({
                            "protocolVersion":"2025-11-25", "capabilities":{},
                            "clientInfo":{"name":"test","version":"1"}
                        }),
                    ))
                    .await
                }
                _ => wire.initialized().await,
            }
            while admission.allowed(&id) {
                tokio::task::yield_now().await;
            }
            let _ = resume.send(());
            if case.starts_with("duplicate") {
                wire.observe_eof().await;
            } else {
                wire.finish().await;
            }
            peer.zero_bytes().await;
            peer.no_connect().await;
            if case == "duplicate-initialized" {
                let new = Admission::default();
                let mut fresh = Wire::open(new.clone(), peer.handler(new));
                fresh.send(request(2, "tools/list", json!({}))).await;
                refused(&fresh.reply(2).await);
                fresh.initialize("2025-11-25").await;
                fresh.initialized().await;
                fresh.send(request(2, "tools/list", json!({}))).await;
                assert_eq!(
                    fresh.reply(2).await["result"]["tools"][0]["name"],
                    "fs.write"
                );
                fresh.finish().await;
                peer.no_connect().await;
            }
        });
    }
}

#[test]
fn wrong_peer_denial_and_uncertain_never_replay() {
    for case in ["wrong-peer", "deny", "uncertain"] {
        run(async {
            let peer = Peer::bind();
            let admission = Admission::default();
            let owner = if case == "wrong-peer" {
                uid() + 1
            } else {
                uid()
            };
            let handler = Handler::recording(admission.clone(), peer.path.clone(), owner);
            let mut wire = Wire::open(admission, handler);
            wire.initialize("2026-07-28").await;
            wire.initialized().await;
            wire.send(call(2)).await;
            if case == "wrong-peer" {
                peer.zero_bytes().await;
            } else {
                let (mut stream, _) = peer.frame().await;
                if case == "deny" {
                    stream
                        .write_all(
                            &writ_rpc::frame::encode(
                                FrameType::Control,
                                br#"{"ok":false,"error":"DENY"}"#,
                            )
                            .unwrap(),
                        )
                        .await
                        .unwrap();
                }
            }
            let response = wire.reply(2).await;
            assert_eq!(response["result"]["isError"], true, "{response}");
            let text = response["result"]["content"][0]["text"].as_str().unwrap();
            assert!(
                text.starts_with(match case {
                    "wrong-peer" => "Writ request was not submitted.",
                    "deny" => "Writ refused the proposal: DENY.",
                    _ => "Writ submission outcome is uncertain.",
                }),
                "{response}"
            );
            wire.finish().await;
            peer.no_connect().await;
        });
    }
}

#[test]
fn cancellation_and_disconnect_after_submission_do_not_forward_again() {
    for cancel in [true, false] {
        run(async {
            let peer = Peer::bind();
            let admission = Admission::default();
            let mut wire = Wire::open(admission.clone(), peer.handler(admission.clone()));
            wire.initialize("2025-11-25").await;
            wire.initialized().await;
            wire.send(call(2)).await;
            let (mut stream, frame) = peer.frame().await;
            assert_eq!(
                frame.as_obj().unwrap()["idempotency_key"].as_str(),
                Some("wire-write-1")
            );
            if cancel {
                wire.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled",
                        "params":{"requestId":2,"reason":"test"}}))
                    .await;
                wire.ping(3).await;
            } else {
                wire.input.take();
            }
            while admission.allowed(&rmcp::model::RequestId::Number(2)) {
                tokio::task::yield_now().await;
            }
            // A recording peer disappearing after submission cannot turn this
            // into a second attempt, even when the MCP response is suppressed.
            assert_ne!(stream.try_read(&mut [0; 1]).ok(), Some(1));
            let _ = stream.shutdown().await;
            drop(stream);
            wire.finish().await;
            peer.no_connect().await;
        });
    }
}
