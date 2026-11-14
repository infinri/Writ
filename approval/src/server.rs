//! Configuration of the production entry point `writ-approval-server` (Y3, D-043; D-040
//! O-1, O-2, O-11, O-12; approval/README.md).
//!
//! Every setting is required and given exactly once on the command line; there is no
//! default, no configuration file, no environment variable and no verifier choice (the
//! entry point always builds [`crate::verifier::PasskeyVerifier`]). Anything missing,
//! repeated, unknown or invalid is a [`ConfigError`], and the entry point exits non-zero
//! before it opens the ledger or binds a socket.
//!
//! - `--ledger <path>`: an existing `writ-ledger-6` ledger (opened through the normal
//!   ownership path; never created here).
//! - `--port <1..65535>`: the loopback port (port 0 is refused: the origin must be known
//!   before binding).
//! - `--rp-id localhost`: the WebAuthn RP id of the local profile ([`crate::listener::RP_ID`]).
//! - `--origin http://localhost:<port>`: exactly the listener's fixed origin for `--port`.
//! - `--channel <identity>`: the channel identity recorded on decisions (O-2).
//! - `--subject-issuer <issuer>`: the human realm of the subjects who sign in on the page.

use std::ffi::OsString;
use std::fmt;
use std::path::PathBuf;

use crate::listener::RP_ID;

/// The command-line synopsis.
pub const USAGE: &str = "usage: writ-approval-server --ledger <path> --port <1..65535> \
--rp-id localhost --origin http://localhost:<port> --channel <identity> --subject-issuer <issuer>";

/// The validated configuration of the entry point.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServerConfig {
    /// The existing ledger file.
    pub ledger: PathBuf,
    /// The loopback port.
    pub port: u16,
    /// The WebAuthn RP id (always [`RP_ID`]).
    pub rp_id: String,
    /// The exact origin `http://localhost:<port>`.
    pub origin: String,
    /// The channel identity recorded on decisions.
    pub channel: String,
    /// The issuer of page subjects.
    pub subject_issuer: String,
}

/// Why a configuration was refused (the entry point exits non-zero).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// A required setting is absent.
    Missing(&'static str),
    /// A setting is given more than once.
    Repeated(&'static str),
    /// An argument that is not a setting (including any verifier or test switch).
    Unknown(String),
    /// A setting without its value.
    NoValue(&'static str),
    /// A value that is not valid UTF-8 or not valid for its setting.
    Invalid(&'static str),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Missing(name) => write!(f, "missing required setting --{name}"),
            ConfigError::Repeated(name) => write!(f, "setting --{name} given more than once"),
            ConfigError::Unknown(arg) => write!(f, "unknown argument {arg:?}"),
            ConfigError::NoValue(name) => write!(f, "setting --{name} has no value"),
            ConfigError::Invalid(name) => write!(f, "invalid value for --{name}"),
        }
    }
}

impl std::error::Error for ConfigError {}

const SETTINGS: [&str; 6] = [
    "ledger",
    "port",
    "rp-id",
    "origin",
    "channel",
    "subject-issuer",
];

/// A channel identity or issuer: 1 to 128 characters from `[A-Za-z0-9._:-]`.
fn identity(text: &str) -> bool {
    !text.is_empty()
        && text.len() <= 128
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
}

impl ServerConfig {
    /// Parse and validate the arguments (without the program name).
    pub fn from_args<I>(args: I) -> Result<ServerConfig, ConfigError>
    where
        I: IntoIterator<Item = OsString>,
    {
        let mut values: [Option<String>; 6] = Default::default();
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            let Some(text) = arg.to_str() else {
                return Err(ConfigError::Unknown(arg.to_string_lossy().into_owned()));
            };
            let Some(index) = text
                .strip_prefix("--")
                .and_then(|name| SETTINGS.iter().position(|s| *s == name))
            else {
                return Err(ConfigError::Unknown(text.to_string()));
            };
            let name = SETTINGS[index];
            if values[index].is_some() {
                return Err(ConfigError::Repeated(name));
            }
            let value = args.next().ok_or(ConfigError::NoValue(name))?;
            let value = value
                .into_string()
                .map_err(|_| ConfigError::Invalid(name))?;
            if value.is_empty() || value.starts_with("--") {
                return Err(ConfigError::NoValue(name));
            }
            values[index] = Some(value);
        }
        let [ledger, port, rp_id, origin, channel, subject_issuer] = values;
        let ledger = ledger.ok_or(ConfigError::Missing("ledger"))?;
        let port = port.ok_or(ConfigError::Missing("port"))?;
        let rp_id = rp_id.ok_or(ConfigError::Missing("rp-id"))?;
        let origin = origin.ok_or(ConfigError::Missing("origin"))?;
        let channel = channel.ok_or(ConfigError::Missing("channel"))?;
        let subject_issuer = subject_issuer.ok_or(ConfigError::Missing("subject-issuer"))?;
        if ledger.contains('\0') {
            return Err(ConfigError::Invalid("ledger"));
        }
        let port = match port.parse::<u16>() {
            Ok(p) if p != 0 && port.bytes().all(|b| b.is_ascii_digit()) => p,
            _ => return Err(ConfigError::Invalid("port")),
        };
        if rp_id != RP_ID {
            return Err(ConfigError::Invalid("rp-id"));
        }
        if origin != format!("http://localhost:{port}") {
            return Err(ConfigError::Invalid("origin"));
        }
        if !identity(&channel) {
            return Err(ConfigError::Invalid("channel"));
        }
        if !identity(&subject_issuer) {
            return Err(ConfigError::Invalid("subject-issuer"));
        }
        Ok(ServerConfig {
            ledger: PathBuf::from(ledger),
            port,
            rp_id,
            origin,
            channel,
            subject_issuer,
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<ServerConfig, ConfigError> {
        ServerConfig::from_args(args.iter().map(OsString::from))
    }

    const GOOD: [&str; 12] = [
        "--ledger",
        "/tmp/l.db",
        "--port",
        "8443",
        "--rp-id",
        "localhost",
        "--origin",
        "http://localhost:8443",
        "--channel",
        "approval-listener",
        "--subject-issuer",
        "writ-local",
    ];

    fn with(name: &str, value: &str) -> Vec<String> {
        let mut out: Vec<String> = GOOD.iter().map(|s| s.to_string()).collect();
        let i = out.iter().position(|a| a == name).unwrap();
        out[i + 1] = value.to_string();
        out
    }

    fn parse_owned(args: &[String]) -> Result<ServerConfig, ConfigError> {
        ServerConfig::from_args(args.iter().map(OsString::from))
    }

    #[test]
    fn a_complete_configuration_parses() {
        let c = parse(&GOOD).unwrap();
        assert_eq!(c.port, 8443);
        assert_eq!(c.origin, "http://localhost:8443");
        assert_eq!(c.rp_id, "localhost");
        assert_eq!(c.channel, "approval-listener");
    }

    #[test]
    fn every_missing_setting_is_refused() {
        for (skip, name) in SETTINGS.iter().enumerate() {
            let args: Vec<String> = GOOD
                .chunks(2)
                .enumerate()
                .filter(|(i, _)| *i != skip)
                .flat_map(|(_, p)| p.iter().map(|s| s.to_string()))
                .collect();
            assert_eq!(
                parse_owned(&args),
                Err(ConfigError::Missing(name)),
                "{name}"
            );
        }
        assert_eq!(parse(&[]), Err(ConfigError::Missing("ledger")));
    }

    #[test]
    fn invalid_values_are_refused() {
        for (name, value, code) in [
            ("--port", "0", "port"),
            ("--port", "+80", "port"),
            ("--port", "65536", "port"),
            ("--rp-id", "example.com", "rp-id"),
            ("--rp-id", "LOCALHOST", "rp-id"),
            ("--origin", "http://localhost:9999", "origin"),
            ("--origin", "https://localhost:8443", "origin"),
            ("--origin", "http://127.0.0.1:8443", "origin"),
            ("--origin", "http://localhost:8443/", "origin"),
            ("--channel", "a b", "channel"),
            ("--subject-issuer", "x/y", "subject-issuer"),
        ] {
            assert_eq!(
                parse_owned(&with(name, value)),
                Err(ConfigError::Invalid(code)),
                "{name} {value}"
            );
        }
    }

    #[test]
    fn unknown_repeated_and_valueless_arguments_are_refused() {
        let mut args: Vec<String> = GOOD.iter().map(|s| s.to_string()).collect();
        args.push("--test-verifier".into());
        assert!(matches!(parse_owned(&args), Err(ConfigError::Unknown(_))));
        let mut args: Vec<String> = GOOD.iter().map(|s| s.to_string()).collect();
        args.extend(["--port".into(), "8443".into()]);
        assert_eq!(parse_owned(&args), Err(ConfigError::Repeated("port")));
        let mut args: Vec<String> = GOOD.iter().map(|s| s.to_string()).collect();
        args.pop();
        assert_eq!(
            parse_owned(&args),
            Err(ConfigError::NoValue("subject-issuer"))
        );
        assert!(matches!(
            parse(&["--ledger=/tmp/x"]),
            Err(ConfigError::Unknown(_))
        ));
        assert_eq!(
            parse_owned(&with("--ledger", "--port")),
            Err(ConfigError::NoValue("ledger"))
        );
    }
}
