use writ_kernel::json::{self, Map, Value};

use crate::{
    valid_label, Authenticator, Change, DraftRef, Error, Mutation, Receipt, Result, Scope, Service,
    Store, Version, MAX_CANDIDATE, MAX_REQUEST,
};

pub const API_VERSION: &str = "/manage/v1";
const MAX_HISTORY: i64 = 50;
const MAX_CORPUS: usize = 16;

pub struct RequestV1 {
    pub(crate) scope: Scope,
    pub(crate) operation: Operation,
    pub(crate) binding: Vec<u8>,
}

pub(crate) enum Operation {
    Mutation(Mutation),
    Get(DraftRef),
    History {
        id: String,
        after: i64,
        limit: usize,
    },
    Validate(DraftRef),
    Simulate {
        draft: DraftRef,
        corpus: Value,
    },
    Compare {
        before: DraftRef,
        after: DraftRef,
        corpus: Value,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub enum ResponseV1 {
    Mutation(Receipt),
    Version(Version),
    History {
        versions: Vec<Version>,
        next: Option<i64>,
    },
    Validation(Value),
    Simulation(Value),
    Comparison(Value),
}

#[derive(Clone, Copy)]
struct Diagnostic {
    code: Error,
    pointer: &'static str,
}

type DecodeResult<T> = std::result::Result<T, Diagnostic>;

fn invalid(pointer: &'static str) -> Diagnostic {
    Diagnostic {
        code: Error::InvalidRequest,
        pointer,
    }
}

impl RequestV1 {
    /// Decode the strict versioned request with the same constraints as the wire boundary.
    pub fn parse(input: &[u8]) -> Result<Self> {
        Self::decode(input).map_err(|diagnostic| diagnostic.code)
    }

    fn decode(input: &[u8]) -> DecodeResult<Self> {
        if input.len() > MAX_REQUEST {
            return Err(Diagnostic {
                code: Error::LimitExceeded,
                pointer: "",
            });
        }
        let value = json::parse(input).map_err(|_| invalid(""))?;
        let mut fields = object(value, "")?;
        if fields.remove("version") != Some(json::s(API_VERSION)) {
            return Err(invalid("/version"));
        }
        let mut scope_fields = object(take(&mut fields, "scope", "/scope")?, "/scope")?;
        let scope = Scope {
            tenant: label(&mut scope_fields, "tenant", "/scope/tenant")?,
            project: label(&mut scope_fields, "project", "/scope/project")?,
        };
        empty(&scope_fields, "/scope")?;
        let operation = label(&mut fields, "operation", "/operation")?;
        let binding = if matches!(operation.as_str(), "create" | "edit" | "clone") {
            // The strict canonical JSON domain binds every field, including operation and scope.
            let value = json::parse(input).map_err(|_| invalid(""))?;
            json::canonical(&value)
                .map_err(|_| invalid(""))?
                .into_bytes()
        } else {
            Vec::new()
        };
        let operation = match operation.as_str() {
            "create" | "edit" | "clone" => decode_mutation(&mut fields, &operation)?,
            "get" => Operation::Get(reference(&mut fields, "draft", "/draft")?),
            "history" => Operation::History {
                id: label(&mut fields, "id", "/id")?,
                after: integer(&mut fields, "after", "/after", 0, json::MAX_SAFE_INTEGER)?,
                limit: integer(&mut fields, "limit", "/limit", 1, MAX_HISTORY)? as usize,
            },
            "validate" => Operation::Validate(reference(&mut fields, "draft", "/draft")?),
            "simulate" => Operation::Simulate {
                draft: reference(&mut fields, "draft", "/draft")?,
                corpus: corpus(&mut fields)?,
            },
            "compare" => Operation::Compare {
                before: reference(&mut fields, "before", "/before")?,
                after: reference(&mut fields, "after", "/after")?,
                corpus: corpus(&mut fields)?,
            },
            _ => return Err(invalid("/operation")),
        };
        empty(&fields, "")?;
        Ok(Self {
            scope,
            operation,
            binding,
        })
    }
}

fn decode_mutation(fields: &mut Map, operation: &str) -> DecodeResult<Operation> {
    let id = label(fields, "id", "/id")?;
    let revisions = if operation == "edit" {
        (1, json::MAX_SAFE_INTEGER)
    } else {
        (0, 0)
    };
    let expected_revision = integer(
        fields,
        "expected_revision",
        "/expected_revision",
        revisions.0,
        revisions.1,
    )?;
    let key = label(fields, "idempotency_key", "/idempotency_key")?;
    let Value::Bool(preview) = take(fields, "preview", "/preview")? else {
        return Err(invalid("/preview"));
    };
    let change = if operation == "clone" {
        Change::Clone(reference(fields, "source", "/source")?)
    } else {
        let candidate = take(fields, "candidate", "/candidate")?;
        if !matches!(candidate, Value::Obj(_)) {
            return Err(invalid("/candidate"));
        }
        if json::canonical(&candidate)
            .map_err(|_| invalid("/candidate"))?
            .len()
            > MAX_CANDIDATE
        {
            return Err(Diagnostic {
                code: Error::LimitExceeded,
                pointer: "/candidate",
            });
        }
        if operation == "edit" {
            Change::Edit(candidate)
        } else {
            Change::Create(candidate)
        }
    };
    Ok(Operation::Mutation(Mutation {
        id,
        expected_revision,
        key,
        preview,
        change,
    }))
}

fn object(value: Value, pointer: &'static str) -> DecodeResult<Map> {
    match value {
        Value::Obj(fields) => Ok(fields),
        _ => Err(invalid(pointer)),
    }
}

fn take(fields: &mut Map, field: &str, pointer: &'static str) -> DecodeResult<Value> {
    fields.remove(field).ok_or_else(|| invalid(pointer))
}

fn empty(fields: &Map, pointer: &'static str) -> DecodeResult<()> {
    if fields.is_empty() {
        Ok(())
    } else {
        Err(invalid(pointer))
    }
}

fn label(fields: &mut Map, field: &str, pointer: &'static str) -> DecodeResult<String> {
    match take(fields, field, pointer)? {
        Value::Str(value) if valid_label(&value) => Ok(value),
        _ => Err(invalid(pointer)),
    }
}

fn integer(
    fields: &mut Map,
    field: &str,
    pointer: &'static str,
    min: i64,
    max: i64,
) -> DecodeResult<i64> {
    match take(fields, field, pointer)? {
        Value::Int(value) if (min..=max).contains(&value) => Ok(value),
        _ => Err(invalid(pointer)),
    }
}

fn reference(fields: &mut Map, field: &str, pointer: &'static str) -> DecodeResult<DraftRef> {
    let mut fields = object(take(fields, field, pointer)?, pointer)?;
    let draft = DraftRef {
        id: label(&mut fields, "id", pointer)?,
        revision: integer(&mut fields, "revision", pointer, 1, json::MAX_SAFE_INTEGER)?,
    };
    empty(&fields, pointer)?;
    Ok(draft)
}

fn corpus(fields: &mut Map) -> DecodeResult<Value> {
    match take(fields, "corpus", "/corpus")? {
        Value::Arr(rows) if rows.len() <= MAX_CORPUS => Ok(Value::Arr(rows)),
        Value::Arr(_) => Err(Diagnostic {
            code: Error::LimitExceeded,
            pointer: "/corpus",
        }),
        _ => Err(invalid("/corpus")),
    }
}

/// Shared byte boundary for the headless fixture and embedding API.
pub fn execute_v1(
    store: &Store,
    authority: Option<&dyn Authenticator>,
    credential: &[u8],
    request: &[u8],
) -> Vec<u8> {
    let service = Service::new(store, authority);
    let principal = match service.authenticate(credential) {
        Ok(principal) => principal,
        Err(error) => return error_response(error),
    };
    let request = match RequestV1::decode(request) {
        Ok(request) => request,
        Err(diagnostic) => return encode_error(diagnostic),
    };
    match service.dispatch(credential, principal, request) {
        Ok(response) => response.encode(),
        Err(error) => error_response(error),
    }
}

impl ResponseV1 {
    pub fn encode(self) -> Vec<u8> {
        let result = match self {
            Self::Mutation(receipt) => json::obj([
                ("revision", Value::Int(receipt.revision)),
                ("audit_id", nullable_int(receipt.audit_id)),
            ]),
            Self::Version(version) => version_value(version),
            Self::History { versions, next } => json::obj([
                (
                    "versions",
                    Value::Arr(versions.into_iter().map(version_value).collect()),
                ),
                ("next", nullable_int(next)),
            ]),
            Self::Validation(value) | Self::Simulation(value) | Self::Comparison(value) => value,
        };
        encode(json::obj([
            ("version", json::s(API_VERSION)),
            ("ok", Value::Bool(true)),
            ("result", result),
        ]))
    }
}

fn version_value(version: Version) -> Value {
    json::obj([
        ("id", json::s(&version.draft.id)),
        ("revision", Value::Int(version.draft.revision)),
        ("digest", json::s(&version.digest)),
        (
            "source",
            version.source.map_or(Value::Null, |draft| {
                json::obj([
                    ("id", json::s(&draft.id)),
                    ("revision", Value::Int(draft.revision)),
                ])
            }),
        ),
    ])
}

fn nullable_int(value: Option<i64>) -> Value {
    value.map_or(Value::Null, Value::Int)
}

pub fn error_response(error: Error) -> Vec<u8> {
    encode_error(Diagnostic {
        code: error,
        pointer: "",
    })
}

fn encode_error(diagnostic: Diagnostic) -> Vec<u8> {
    let (code, message) = match diagnostic.code {
        Error::InvalidRequest => ("INVALID_REQUEST", "Request does not match the v1 contract."),
        Error::Unauthorized => ("UNAUTHORIZED", "Management authentication is required."),
        Error::NotFound => (
            "NOT_FOUND",
            "The requested resource or operation is unavailable in this scope.",
        ),
        Error::RevisionConflict => (
            "REVISION_CONFLICT",
            "Fetch the current revision and submit a new mutation.",
        ),
        Error::IdempotencyConflict => (
            "IDEMPOTENCY_CONFLICT",
            "Use the original request or a new idempotency key.",
        ),
        Error::LimitExceeded => (
            "LIMIT_EXCEEDED",
            "Reduce the request or use a fresh disposable store.",
        ),
        Error::Unavailable => ("UNAVAILABLE", "The operation could not be completed."),
        Error::InvalidPolicy => (
            "INVALID_POLICY",
            "Validate the specified draft revision for diagnostics.",
        ),
    };
    encode(json::obj([
        ("version", json::s(API_VERSION)),
        ("ok", Value::Bool(false)),
        (
            "error",
            json::obj([
                ("code", json::s(code)),
                ("message", json::s(message)),
                ("pointer", json::s(diagnostic.pointer)),
            ]),
        ),
    ]))
}

fn encode(value: Value) -> Vec<u8> {
    json::canonical(&value).map_or_else(
        |_| br#"{"error":{"code":"UNAVAILABLE","message":"The operation could not be completed.","pointer":""},"ok":false,"version":"/manage/v1"}"#.to_vec(),
        String::into_bytes,
    )
}
