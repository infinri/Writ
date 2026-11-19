//! Exercise the executable over piped standard input and output.
#![cfg(target_os = "linux")]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use serde_json::Value;
use std::process::{Child, Command, Stdio};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::pipe::{Receiver, Sender};

struct Cleanup(Child);

impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn executable_has_sdk_stdio_but_no_governed_backend() {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(8), exercise())
                .await
                .expect("stdio test deadline");
        });
}

async fn exercise() {
    let mut child = Cleanup(
        Command::new(env!("CARGO_BIN_EXE_writ-mcp-shim"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut input = Sender::from_owned_fd(child.0.stdin.take().unwrap().into()).unwrap();
    let mut output =
        BufReader::new(Receiver::from_owned_fd(child.0.stdout.take().unwrap().into()).unwrap());
    input.write_all(concat!(
        r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"stdio-test","version":"1"}}}"#,
        "\n"
    ).as_bytes()).await.unwrap();
    assert_eq!(
        reply(&mut output).await["result"]["protocolVersion"],
        "2025-11-25"
    );
    input
        .write_all(
            concat!(
                r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
                "\n",
                r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
                "\n"
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    assert_eq!(
        reply(&mut output).await["result"]["tools"],
        serde_json::json!([])
    );
    let effect = serde_json::json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
        "name":"fs.write",
        "arguments":{
            "relative_path":"result.txt",
            "source_path":"source.txt",
            "snapshot_id":format!("snap-{}", "a".repeat(64)),
            "manifest_digest":format!("sha256:{}", "b".repeat(64)),
            "content_digest":format!("sha256:{}", "c".repeat(64)),
            "idempotency_key":"stdio-closed"
        }
    }});
    input
        .write_all(format!("{effect}\n").as_bytes())
        .await
        .unwrap();
    let response = reply(&mut output).await;
    assert_eq!(response["result"]["isError"], true, "{response}");
    assert_eq!(
        response["result"]["content"][0]["text"],
        "Governed activation is unavailable."
    );
    drop(input);
    assert_eq!(output.read_line(&mut String::new()).await.unwrap(), 0);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success(), "{status}");
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

async fn reply(output: &mut BufReader<Receiver>) -> Value {
    let mut line = String::new();
    assert_ne!(output.read_line(&mut line).await.unwrap(), 0);
    serde_json::from_str(&line).unwrap()
}
