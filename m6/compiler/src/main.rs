//! Local invocation for the M6 synthetic authorization compiler.

use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::process::ExitCode;

use writ_kernel::json;
use writ_policy_compile::evaluate;

fn run() -> Result<bool, String> {
    let mut args = env::args_os().skip(1);
    let Some(path) = args.next().filter(|_| args.next().is_none()) else {
        return Err("usage: writ-policy-compile <input.json|->".into());
    };
    let input = if path == "-" {
        let mut input = Vec::new();
        io::stdin().read_to_end(&mut input).map(|_| input)
    } else {
        fs::read(path)
    }
    .map_err(|error| format!("input read failed ({})", error.kind()))?;

    let report = evaluate(&input);
    let canonical = json::canonical(&report.document)
        .map_err(|error| format!("report canonicalization failed ({error})"))?;
    writeln!(io::stdout().lock(), "{canonical}")
        .map_err(|error| format!("report write failed ({})", error.kind()))?;
    Ok(report.accepted)
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(2)
        }
    }
}
