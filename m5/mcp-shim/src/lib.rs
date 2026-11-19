//! Development-only agent-side MCP shim (M5b-02, M5b-03).

#![cfg(target_os = "linux")]

mod handler;
mod lifecycle;

#[cfg(test)]
mod wire_tests;

use handler::Handler;
use lifecycle::{Admission, Ordered};
use rmcp::transport::async_rw::AsyncRwTransport;
use rmcp::{RoleServer, ServiceExt};
use tokio::io::{AsyncRead, AsyncWrite};

async fn serve(
    read: impl AsyncRead + Send + Unpin + 'static,
    write: impl AsyncWrite + Send + Unpin + 'static,
    admission: Admission,
    handler: Handler,
) -> Result<(), &'static str> {
    let transport =
        AsyncRwTransport::<RoleServer, _, _>::new(read, write).with_max_line_length(64 * 1024);
    let result = match handler
        .serve(Ordered::new(transport, admission.clone()))
        .await
    {
        Ok(service) => service
            .waiting()
            .await
            .map(|_| ())
            .map_err(|_| "SDK task failed"),
        Err(_) => Err("MCP session refused during initialization"),
    };
    admission.close();
    result
}

/// Serve MCP on stdin and stdout. Effect activation is unavailable pending M5a verification.
///
/// # Errors
///
/// Returns a diagnostic if the runtime or SDK session fails.
pub fn run_stdio() -> Result<(), &'static str> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| "cannot start SDK runtime")?
        .block_on(async {
            // Reopen through the pipe driver so a closed SDK session never waits
            // for an outstanding blocking read on the original standard input.
            let pipes = tokio::net::unix::pipe::OpenOptions::new();
            let read = pipes
                .open_receiver("/proc/self/fd/0")
                .map_err(|_| "stdin must be a usable pipe")?;
            let write = pipes
                .open_sender("/proc/self/fd/1")
                .map_err(|_| "stdout must be a usable pipe")?;
            let admission = Admission::default();
            serve(read, write, admission.clone(), Handler::closed(admission)).await
        })
}
