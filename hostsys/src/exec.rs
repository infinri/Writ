//! Bounded child-process execution for probes: cleared environment, descriptors 0..2 only
//! (the standard library opens everything else close-on-exec), its own process group, a
//! deadline and capped output.

use std::ffi::OsStr;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

/// Largest amount of stdout or stderr kept from one child.
pub const OUTPUT_CAP: usize = 64 * 1024;

/// The observed end of a child process.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// Exit code, `None` when killed by a signal or at the deadline.
    pub code: Option<i32>,
    /// Captured stdout (lossy UTF-8, capped).
    pub stdout: String,
    /// Captured stderr (lossy UTF-8, capped).
    pub stderr: String,
    /// The deadline passed and the child was killed.
    pub timed_out: bool,
}

impl Outcome {
    /// Exit code zero.
    pub fn success(&self) -> bool {
        self.code == Some(0)
    }

    /// First non-empty stderr line (for reasons), else a description of the exit.
    pub fn diagnostic(&self) -> String {
        let line = self.stderr.lines().map(str::trim).find(|l| !l.is_empty());
        match (line, self.timed_out, self.code) {
            (_, true, _) => "timed out".to_string(),
            (Some(l), _, _) => l.to_string(),
            (None, _, Some(c)) => format!("exit status {c}"),
            (None, _, None) => "terminated by a signal".to_string(),
        }
    }
}

/// A command with a cleared environment plus `PATH=/usr/bin:/bin`, in its own process group
/// (so a deadline kills that group, never the probe's own group).
pub fn command<S: AsRef<OsStr>>(argv: &[S]) -> Option<Command> {
    let (program, args) = argv.split_first()?;
    let mut c = Command::new(program);
    c.args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .stdin(Stdio::null())
        .process_group(0);
    Some(c)
}

fn drain<R: Read + Send + 'static>(r: Option<R>) -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut kept = Vec::new();
        if let Some(mut r) = r {
            let mut buf = [0u8; 4096];
            while let Ok(n) = r.read(&mut buf) {
                if n == 0 {
                    break;
                }
                let room = OUTPUT_CAP.saturating_sub(kept.len());
                kept.extend_from_slice(&buf[..n.min(room)]);
            }
        }
        let _ = tx.send(String::from_utf8_lossy(&kept).into_owned());
    });
    rx
}

fn collect(rx: &mpsc::Receiver<String>) -> String {
    rx.recv_timeout(Duration::from_secs(2))
        .unwrap_or_else(|_| "(output incomplete: a descendant still holds the pipe)".to_string())
}

/// SIGKILL the child's process group (the child leads it, see [`command`]) and the child.
pub fn kill_group(child: &mut Child) {
    let group = i32::try_from(child.id())
        .ok()
        .and_then(rustix::process::Pid::from_raw);
    if let Some(pid) = group {
        let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
    }
    let _ = child.kill();
}

/// Wait for a spawned child with captured stdout and stderr until `deadline`, killing its
/// process group then.
pub fn wait(mut child: Child, deadline: Duration) -> std::io::Result<Outcome> {
    let out = drain(child.stdout.take());
    let err = drain(child.stderr.take());
    let start = Instant::now();
    let mut timed_out = false;
    let status = loop {
        if let Some(st) = child.try_wait()? {
            break st;
        }
        if start.elapsed() >= deadline {
            timed_out = true;
            kill_group(&mut child);
            break child.wait()?;
        }
        thread::sleep(Duration::from_millis(10));
    };
    Ok(Outcome {
        code: if timed_out { None } else { status.code() },
        stdout: collect(&out),
        stderr: collect(&err),
        timed_out,
    })
}

/// Run `cmd` to completion with `stdin` bytes (or no input) and a deadline.
pub fn run(mut cmd: Command, stdin: Option<&[u8]>, deadline: Duration) -> std::io::Result<Outcome> {
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    if stdin.is_some() {
        cmd.stdin(Stdio::piped());
    }
    let mut child = cmd.spawn()?;
    if let (Some(bytes), Some(mut pipe)) = (stdin, child.stdin.take()) {
        let _ = pipe.write_all(bytes);
    }
    wait(child, deadline)
}

/// Run `argv` (cleared environment, no input) with a deadline.
pub fn run_argv<S: AsRef<OsStr>>(argv: &[S], deadline: Duration) -> std::io::Result<Outcome> {
    match command(argv) {
        Some(c) => run(c, None, deadline),
        None => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "empty argv",
        )),
    }
}

/// `key=value` lines of a job's output, in order (lines without `=` are ignored).
pub fn kv(text: &str) -> Vec<(String, String)> {
    text.lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect()
}

/// The last value of `key` in parsed `key=value` output.
pub fn kv_get<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs
        .iter()
        .rev()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn kv_parses_last_value_and_ignores_noise() {
        let p = kv("noise\na=1\nb = two \na=3\n");
        assert_eq!(kv_get(&p, "a"), Some("3"));
        assert_eq!(kv_get(&p, "b"), Some("two"));
        assert_eq!(kv_get(&p, "c"), None);
    }

    #[test]
    fn runs_with_cleared_environment_and_deadline() {
        let o = run_argv(
            &["/bin/sh", "-c", "echo \"x=$HOME\"; echo err >&2; exit 3"],
            Duration::from_secs(10),
        )
        .unwrap();
        assert_eq!(o.code, Some(3));
        assert_eq!(kv_get(&kv(&o.stdout), "x"), Some(""));
        assert_eq!(o.diagnostic(), "err");
        let started = Instant::now();
        let t = run_argv(
            &["/bin/sh", "-c", "sleep 30; echo late"],
            Duration::from_millis(100),
        )
        .unwrap();
        assert!(t.timed_out);
        assert_eq!(t.code, None);
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "group kill reaped the grandchild"
        );
    }
}
