use std::{
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
};

use writ_kernel::json::{self, Value};

use crate::{Error, Result};

const MAX_DOCUMENT: usize = 16 * 1024;
const MAX_VALIDATOR_OUTPUT: usize = 64 * 1024;
const VALIDATOR: &str = include_str!("contract_validation.py");

pub struct ContractValidator {
    executable: PathBuf,
}

pub(crate) struct Validation {
    pub diagnostics: Value,
    pub normalized: Option<Value>,
}

impl ContractValidator {
    /// The executable and its package environment are supplied by the registry owner.
    pub fn new(executable: impl Into<PathBuf>) -> Result<Self> {
        let executable = executable.into();
        if !executable.is_absolute() {
            return Err(Error::Unavailable);
        }
        Ok(Self { executable })
    }

    #[cfg(any(test, feature = "fixtures"))]
    pub fn from_fixture_environment() -> Result<Self> {
        Self::new(
            std::env::var_os("WRIT_CONTRACT_PYTHON")
                .map_or_else(|| PathBuf::from("/usr/bin/python3"), PathBuf::from),
        )
    }

    pub(crate) fn validate_schema(&self, schema: &Value) -> Result<Validation> {
        self.validate(schema, None, false)
    }

    pub(crate) fn validate_example(
        &self,
        schema: &Value,
        example: &Value,
        core: bool,
    ) -> Result<Value> {
        Ok(self.validate(schema, Some(example), core)?.diagnostics)
    }

    fn validate(&self, schema: &Value, example: Option<&Value>, core: bool) -> Result<Validation> {
        if [Some(schema), example]
            .into_iter()
            .flatten()
            .map(|value| json::canonical(value).map_err(|_| Error::InvalidRequest))
            .try_fold(false, |oversize, doc| {
                doc.map(|doc| oversize || doc.len() > MAX_DOCUMENT)
            })?
        {
            return Err(Error::LimitExceeded);
        }
        let mut body = json::obj([("schema", schema.clone()), ("core", Value::Bool(core))]);
        if let (Some(example), Value::Obj(fields)) = (example, &mut body) {
            fields.insert("example".to_owned(), example.clone());
        }
        let body = json::canonical(&body).map_err(|_| Error::InvalidRequest)?;
        let mut process = Command::new(&self.executable)
            .args(["-I", "-B", "-c", VALIDATOR])
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| Error::Unavailable)?;
        let sent = process
            .stdin
            .take()
            .ok_or(Error::Unavailable)
            .and_then(|mut pipe| {
                pipe.write_all(body.as_bytes())
                    .map_err(|_| Error::Unavailable)
            });
        let output = process.wait_with_output().map_err(|_| Error::Unavailable)?;
        sent?;
        if !output.status.success() || output.stdout.len() > MAX_VALIDATOR_OUTPUT {
            return Err(Error::Unavailable);
        }
        let Value::Obj(mut output) = json::parse(&output.stdout).map_err(|_| Error::Unavailable)?
        else {
            return Err(Error::Unavailable);
        };
        let Some(diagnostics @ Value::Arr(_)) = output.remove("diagnostics") else {
            return Err(Error::Unavailable);
        };
        let normalized = match output.remove("normalized") {
            Some(Value::Null) => None,
            Some(normalized @ Value::Obj(_)) => Some(normalized),
            _ => return Err(Error::Unavailable),
        };
        if !output.is_empty()
            || (example.is_some() && normalized.is_some())
            || (example.is_none()
                && (normalized.is_some()
                    != matches!(&diagnostics, Value::Arr(items) if items.is_empty())))
        {
            return Err(Error::Unavailable);
        }
        Ok(Validation {
            diagnostics,
            normalized,
        })
    }
}
