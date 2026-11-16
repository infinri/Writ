//! `writ-job-launcher`: one root instance per `launch.sock` connection
//! (`writ-job-launcher@.service`, `Accept=yes`, `StandardInput=socket`; J-11.2). Takes no
//! arguments except `--build-info`. Refuses to run unless built with `panic = "abort"`
//! (profile `launcher`). Runs only in the designated disposable VM (M5-D12).

use std::io::Write;
use std::process::ExitCode;
use writ_containment::config::Paths;
use writ_containment::{build_info, launcher, PANIC_ABORT};

fn main() -> ExitCode {
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if args.len() == 1 && args[0] == "--build-info" {
        let _ = std::io::stdout().write_all(build_info().as_bytes());
        return ExitCode::SUCCESS;
    }
    if !args.is_empty() {
        let _ = writeln!(std::io::stderr(), "writ-job-launcher: takes no arguments");
        return ExitCode::from(64);
    }
    if !PANIC_ABORT {
        let _ = writeln!(
            std::io::stderr(),
            "writ-job-launcher: refusing to run: not built with panic = \"abort\" (profile launcher)"
        );
        return ExitCode::from(70);
    }
    ExitCode::from(launcher::serve(&Paths::reference()) as u8)
}
