//! Run the closed development shim over SDK stdio.

#[cfg(target_os = "linux")]
fn main() -> std::process::ExitCode {
    match writ_mcp_shim::run_stdio() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("writ-mcp-shim: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn main() -> std::process::ExitCode {
    eprintln!("writ-mcp-shim requires Linux");
    std::process::ExitCode::FAILURE
}
