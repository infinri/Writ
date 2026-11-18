//! Guest-only desktop inventory command.

#[cfg(target_os = "linux")]
fn main() -> std::process::ExitCode {
    if std::thread::Builder::new()
        .name("inventory-deadline".into())
        .spawn(|| {
            std::thread::sleep(std::time::Duration::from_secs(16));
            std::process::exit(3);
        })
        .is_err()
    {
        eprintln!("desktop inventory unavailable: cannot start deadline watchdog");
        return std::process::ExitCode::from(3);
    }
    match writ_hostsys::run_guest_inventory(std::env::args_os().skip(1)) {
        Ok(inventory) => {
            use std::io::Write;
            match std::io::stdout().lock().write_all(inventory.as_bytes()) {
                Ok(()) => std::process::ExitCode::SUCCESS,
                Err(_) => std::process::ExitCode::from(1),
            }
        }
        Err(reason) => {
            eprintln!("{reason}");
            std::process::ExitCode::from(3)
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn main() -> std::process::ExitCode {
    eprintln!("desktop inventory unavailable: Linux reference guest required");
    std::process::ExitCode::from(3)
}
