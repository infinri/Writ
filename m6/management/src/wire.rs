use writ_kernel::{
    ids::{is_digest, is_id, is_name},
    json::{self, Map, Value},
};
use writ_policy_publication_lab::Publication;

use crate::{
    valid_label, Authenticator, Change, ContractOperation, DraftRef, Error, EvaluatorOperation,
    Mutation, PackOperation, PublicationRequest, Receipt, Result, Scope, Service, Store, Version,
    WaiverOperation, MAX_CANDIDATE, MAX_REQUEST,
};

pub const API_VERSION: &str = "/manage/v1";
const MAX_HISTORY: i64 = 50;
const MAX_CORPUS: usize = 16;
const MAX_PUBLICATION_VERSION: i64 = 999_999_999;
const MAX_PRESENTATION: usize = 512;

pub struct RequestV1 {
    pub(crate) scope: Scope,
    pub(crate) operation: Operation,
    pub(crate) binding: Vec<u8>,
}

pub(crate) enum Operation {
    Publication(PublicationRequest),
    Evaluator(EvaluatorOperation, Map),
    Contract(ContractOperation, Map),
    Pack(PackOperation, Map),
    Waiver(WaiverOperation, Map),
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
    Publication(Value),
    Evaluator(Value),
    Contract(Value),
    Pack(Value),
    Waiver(Value),
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
    /// Decode the strict versioned envelope and operation fields.
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
            "preview_publish"
            | "preview_rollback"
            | "preview_retire"
            | "preview_deactivate"
            | "publish"
            | "rollback"
            | "retire"
            | "deactivate"
            | "publication_history" => {
                Operation::Publication(decode_publication(&mut fields, &operation)?)
            }
            "evaluator_inspect" => {
                Operation::Evaluator(EvaluatorOperation::Inspect, std::mem::take(&mut fields))
            }
            "evaluator_preview_publish" => Operation::Evaluator(
                EvaluatorOperation::PreviewPublish,
                std::mem::take(&mut fields),
            ),
            "evaluator_preview_revoke" => Operation::Evaluator(
                EvaluatorOperation::PreviewRevoke,
                std::mem::take(&mut fields),
            ),
            "evaluator_publish" => {
                Operation::Evaluator(EvaluatorOperation::Publish, std::mem::take(&mut fields))
            }
            "evaluator_revoke" => {
                Operation::Evaluator(EvaluatorOperation::Revoke, std::mem::take(&mut fields))
            }
            "evaluator_recover" => {
                Operation::Evaluator(EvaluatorOperation::Recover, std::mem::take(&mut fields))
            }
            "waiver_request" => {
                Operation::Waiver(WaiverOperation::Request, std::mem::take(&mut fields))
            }
            "waiver_begin" => {
                Operation::Waiver(WaiverOperation::Begin, std::mem::take(&mut fields))
            }
            "waiver_finish" => {
                Operation::Waiver(WaiverOperation::Finish, std::mem::take(&mut fields))
            }
            "waiver_inspect" => {
                Operation::Waiver(WaiverOperation::Inspect, std::mem::take(&mut fields))
            }
            "waiver_validate" => {
                Operation::Waiver(WaiverOperation::Validate, std::mem::take(&mut fields))
            }
            "waiver_recover" => {
                Operation::Waiver(WaiverOperation::Recover, std::mem::take(&mut fields))
            }
            "contracts.browse" => contract_operation(ContractOperation::Browse, &mut fields)?,
            "contracts.inspect" => contract_operation(ContractOperation::Inspect, &mut fields)?,
            "contracts.validate-schema" => {
                contract_operation(ContractOperation::ValidateSchema, &mut fields)?
            }
            "contracts.validate-example" => {
                contract_operation(ContractOperation::ValidateExample, &mut fields)?
            }
            "contracts.create" => contract_operation(ContractOperation::Create, &mut fields)?,
            "contracts.preview" => contract_operation(ContractOperation::Preview, &mut fields)?,
            "contracts.publish" => contract_operation(ContractOperation::Publish, &mut fields)?,
            "contracts.deprecate" => contract_operation(ContractOperation::Deprecate, &mut fields)?,
            "contracts.retire" => contract_operation(ContractOperation::Retire, &mut fields)?,
            "contracts.recover" => contract_operation(ContractOperation::Recover, &mut fields)?,
            "packs.install" => pack_operation(PackOperation::Install, &mut fields)?,
            "packs.verify" => pack_operation(PackOperation::Verify, &mut fields)?,
            "packs.inspect" => pack_operation(PackOperation::Inspect, &mut fields)?,
            "packs.enable" => pack_operation(PackOperation::Enable, &mut fields)?,
            "packs.disable" => pack_operation(PackOperation::Disable, &mut fields)?,
            "packs.upgrade" => pack_operation(PackOperation::Upgrade, &mut fields)?,
            "packs.history" => pack_operation(PackOperation::History, &mut fields)?,
            "packs.preview" => pack_operation(PackOperation::Preview, &mut fields)?,
            "packs.recover" => pack_operation(PackOperation::Recover, &mut fields)?,
            _ => return Err(invalid("/operation")),
        };
        empty(&fields, "")?;
        match &operation {
            Operation::Evaluator(kind, fields) => check_lifecycle(fields, evaluator_fields(*kind))?,
            Operation::Waiver(kind, fields) => check_lifecycle(fields, waiver_fields(*kind))?,
            _ => {}
        }
        Ok(Self {
            scope,
            operation,
            binding,
        })
    }
}

enum LifecycleField {
    Label,
    Id,
    Name,
    Digest,
    Number(i64, i64),
    Presentation(usize),
    Reason,
    Entry,
    AnyJson,
}

impl LifecycleField {
    fn accepts(&self, value: &Value) -> bool {
        match self {
            Self::Label => value.as_str().is_some_and(valid_label),
            Self::Id => value.as_str().is_some_and(is_id),
            Self::Name => value.as_str().is_some_and(is_name),
            Self::Digest => value.as_str().is_some_and(is_digest),
            Self::Number(min, max) => value.as_int().is_some_and(|n| (*min..=*max).contains(&n)),
            Self::Presentation(max) => value
                .as_str()
                .is_some_and(|s| !s.is_empty() && s.len() <= *max),
            Self::Reason => value.as_str().is_some_and(|s| {
                !s.trim().is_empty() && s.chars().count() <= 500 && !s.chars().any(char::is_control)
            }),
            Self::Entry => writ_broker::contracts::RegistryEntry::validate(value).is_ok(),
            Self::AnyJson => true,
        }
    }
}

type LifecycleFields = &'static [(&'static str, &'static str, LifecycleField)];

fn check_lifecycle(
    fields: &Map,
    contract: &[(&str, &'static str, LifecycleField)],
) -> DecodeResult<()> {
    for (name, pointer, domain) in contract {
        match fields.get(*name) {
            Some(value) if domain.accepts(value) => {}
            _ => return Err(invalid(pointer)),
        }
    }
    if fields.len() != contract.len() {
        return Err(invalid(""));
    }
    Ok(())
}

fn pack_operation(operation: PackOperation, fields: &mut Map) -> DecodeResult<Operation> {
    use LifecycleField::{AnyJson, Digest, Label, Number, Presentation};
    use PackOperation::*;
    let mut required = Vec::new();
    if operation != Recover {
        required.push(("pack_id", "/pack_id", Presentation(64)));
        if operation != History {
            required.push(("pack_version", "/pack_version", Presentation(32)));
        }
    }
    let change = if operation == Preview {
        required.push(("operation", "/pack_operation", Presentation(8)));
        let change = fields
            .get("pack_operation")
            .and_then(Value::as_str)
            .and_then(|name| PackOperation::change(name).ok())
            .ok_or_else(|| invalid("/pack_operation"))?;
        let value = fields
            .remove("pack_operation")
            .ok_or_else(|| invalid("/pack_operation"))?;
        fields.insert("operation".into(), value);
        change
    } else {
        operation
    };
    if !change.read_only() {
        required.push(("expected_revision", "/expected_revision", Number(0, 512)));
        if change == Install {
            required.push(("envelope", "/envelope", AnyJson));
        }
    }
    if !operation.read_only() || operation == Recover {
        required.push(("idempotency_key", "/idempotency_key", Label));
    }
    if !operation.read_only() {
        required.push(("intent_digest", "/intent_digest", Digest));
        required.push(("presentation", "/presentation", Presentation(512)));
        if operation.destructive() {
            required.push(("confirmation", "/confirmation", Presentation(192)));
        }
    }
    check_lifecycle(fields, &required)?;
    Ok(Operation::Pack(operation, std::mem::take(fields)))
}

fn contract_operation(operation: ContractOperation, fields: &mut Map) -> DecodeResult<Operation> {
    use ContractOperation::*;
    use LifecycleField::*;

    const ID: (&str, &str, LifecycleField) = ("contract_id", "/contract_id", Presentation(192));
    const VERSION: (&str, &str, LifecycleField) =
        ("contract_version", "/contract_version", Presentation(32));
    const REPLACEMENT: (&str, &str, LifecycleField) = (
        "replacement_version",
        "/replacement_version",
        Presentation(32),
    );
    const END: (&str, &str, LifecycleField) = ("end_date", "/end_date", Presentation(10));
    const REVISION: (&str, &str, LifecycleField) = (
        "expected_revision",
        "/expected_revision",
        Number(0, json::MAX_SAFE_INTEGER - 1),
    );
    const KEY: (&str, &str, LifecycleField) = ("idempotency_key", "/idempotency_key", Label);
    const DIGEST: (&str, &str, LifecycleField) = ("intent_digest", "/intent_digest", Digest);
    const PROOF: (&str, &str, LifecycleField) =
        ("presentation", "/presentation", Presentation(512));
    const CONFIRM: (&str, &str, LifecycleField) =
        ("confirmation", "/confirmation", Presentation(256));
    const SCHEMA: (&str, &str, LifecycleField) = ("schema", "/schema", AnyJson);
    const EXAMPLE: (&str, &str, LifecycleField) = ("example", "/example", AnyJson);
    const CHANGE: (&str, &str, LifecycleField) = ("change", "/change", Label);
    const ACK: &str = "acknowledge_self_publication";

    let change = fields.get("change").and_then(Value::as_str);
    let required: LifecycleFields = match operation {
        Browse => &[],
        Inspect => &[ID],
        ValidateSchema => &[SCHEMA],
        ValidateExample => &[ID, VERSION, EXAMPLE],
        Create => &[ID, VERSION, SCHEMA, REVISION, KEY],
        Preview => match change {
            Some("publish" | "retire") => &[CHANGE, ID, VERSION, REVISION],
            Some("deprecate") => &[CHANGE, ID, VERSION, REVISION, REPLACEMENT, END],
            _ => return Err(invalid("/change")),
        },
        Publish => &[ID, VERSION, REVISION, KEY, DIGEST, PROOF],
        Deprecate => &[
            ID,
            VERSION,
            REVISION,
            REPLACEMENT,
            END,
            KEY,
            DIGEST,
            PROOF,
            CONFIRM,
        ],
        Retire => &[ID, VERSION, REVISION, KEY, DIGEST, PROOF, CONFIRM],
        Recover => &[KEY],
    };
    let ack = if operation == Publish || (operation == Preview && change == Some("publish")) {
        fields.remove(ACK)
    } else {
        None
    };
    if ack.as_ref().is_some_and(|value| value.as_bool().is_none()) {
        return Err(invalid("/acknowledge_self_publication"));
    }
    check_lifecycle(fields, required)?;
    if let Some(ack) = ack {
        fields.insert(ACK.into(), ack);
    }
    let mut fields = std::mem::take(fields);
    if let Some(version) = fields.remove("contract_version") {
        fields.insert("version".into(), version);
    }
    Ok(Operation::Contract(operation, fields))
}

fn evaluator_fields(operation: EvaluatorOperation) -> LifecycleFields {
    use EvaluatorOperation::*;
    use LifecycleField::*;

    const REVISION: (&str, &str, LifecycleField) = (
        "expected_revision",
        "/expected_revision",
        Number(0, json::MAX_SAFE_INTEGER - 1),
    );
    const ENTRY: (&str, &str, LifecycleField) = ("entry", "/entry", Entry);
    const ID: (&str, &str, LifecycleField) = ("evaluator_id", "/evaluator_id", Name);
    const KEY: (&str, &str, LifecycleField) = ("idempotency_key", "/idempotency_key", Label);
    const DIGEST: (&str, &str, LifecycleField) = ("intent_digest", "/intent_digest", Digest);
    const PROOF: (&str, &str, LifecycleField) =
        ("presentation", "/presentation", Presentation(512));
    match operation {
        Inspect => &[
            ID,
            ("after", "/after", Number(0, json::MAX_SAFE_INTEGER)),
            ("limit", "/limit", Number(1, MAX_HISTORY)),
        ],
        PreviewPublish => &[REVISION, ENTRY],
        PreviewRevoke => &[REVISION, ID],
        Publish => &[REVISION, ENTRY, KEY, DIGEST, PROOF],
        Revoke => &[REVISION, ID, KEY, DIGEST, PROOF],
        Recover => &[KEY],
    }
}

fn waiver_fields(operation: WaiverOperation) -> LifecycleFields {
    use LifecycleField::*;
    use WaiverOperation::*;

    const TRANSITION: (&str, &str, LifecycleField) = ("transition_ref", "/transition_ref", Id);
    const WAIVER: (&str, &str, LifecycleField) = ("waiver_id", "/waiver_id", Digest);
    const KEY: (&str, &str, LifecycleField) = ("idempotency_key", "/idempotency_key", Label);
    const DISPATCH: (&str, &str, LifecycleField) = ("dispatch_key", "/dispatch_key", Label);
    match operation {
        Request => &[
            TRANSITION,
            ("workflow_id", "/workflow_id", Name),
            ("from_state", "/from_state", Name),
            ("to_state", "/to_state", Name),
            (
                "expected_revision",
                "/expected_revision",
                Number(0, json::MAX_SAFE_INTEGER),
            ),
            ("criterion_id", "/criterion_id", Name),
            ("reason", "/reason", Reason),
        ],
        Begin => &[
            TRANSITION,
            WAIVER,
            KEY,
            ("approver_issuer", "/approver_issuer", Label),
            ("approver_subject", "/approver_subject", Label),
        ],
        Finish => &[
            TRANSITION,
            WAIVER,
            KEY,
            DISPATCH,
            ("challenge_id", "/challenge_id", Id),
            ("presentation", "/presentation", Presentation(16 * 1024)),
        ],
        Inspect => &[WAIVER],
        Validate => &[TRANSITION, WAIVER],
        Recover => &[DISPATCH],
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

fn decode_publication(fields: &mut Map, operation: &str) -> DecodeResult<PublicationRequest> {
    if operation == "publication_history" {
        return Ok(PublicationRequest::History {
            after: integer(fields, "after", "/after", 0, json::MAX_SAFE_INTEGER)?,
            limit: integer(fields, "limit", "/limit", 1, MAX_HISTORY)? as usize,
        });
    }
    let expected_revision = integer(
        fields,
        "expected_revision",
        "/expected_revision",
        0,
        json::MAX_SAFE_INTEGER,
    )?;
    let publication_version = integer(
        fields,
        "publication_version",
        "/publication_version",
        1,
        MAX_PUBLICATION_VERSION,
    )?;
    if matches!(operation, "preview_deactivate" | "deactivate") {
        if fields.contains_key("publication") {
            let draft = reference(fields, "draft", "/draft")?;
            let publication = take(fields, "publication", "/publication")?;
            let candidate =
                Publication::from_value(&publication).map_err(|_| invalid("/publication"))?;
            let Value::Bool(acknowledge_self) =
                take(fields, "acknowledge_self", "/acknowledge_self")?
            else {
                return Err(invalid("/acknowledge_self"));
            };
            return Ok(PublicationRequest::OptionalRemoval {
                draft,
                expected_revision,
                publication_version,
                candidate: Box::new(candidate),
                policy_id: label(fields, "policy_id", "/policy_id")?,
                confirmation: confirmation(fields)?,
                acknowledge_self,
                key: label(fields, "idempotency_key", "/idempotency_key")?,
                approval: if operation == "deactivate" {
                    Some(presentation(fields)?)
                } else {
                    None
                },
            });
        }
        return Ok(PublicationRequest::Deactivate {
            expected_revision,
            publication_version,
            policy_id: label(fields, "policy_id", "/policy_id")?,
            confirmation: confirmation(fields)?,
        });
    }
    let Value::Bool(acknowledge_self) = take(fields, "acknowledge_self", "/acknowledge_self")?
    else {
        return Err(invalid("/acknowledge_self"));
    };
    let key = label(fields, "idempotency_key", "/idempotency_key")?;
    if matches!(operation, "preview_publish" | "publish") {
        let draft = reference(fields, "draft", "/draft")?;
        let publication = take(fields, "publication", "/publication")?;
        let candidate =
            Publication::from_value(&publication).map_err(|_| invalid("/publication"))?;
        if operation == "preview_publish" {
            return Ok(PublicationRequest::PreviewPublish {
                draft,
                expected_revision,
                publication_version,
                candidate,
                acknowledge_self,
                key,
            });
        }
        let (intent_digest, presentation) = presentation(fields)?;
        return Ok(PublicationRequest::Publish {
            draft,
            expected_revision,
            publication_version,
            candidate,
            acknowledge_self,
            intent_digest,
            key,
            presentation,
        });
    }
    if matches!(operation, "preview_retire" | "retire") {
        let confirmation = confirmation(fields)?;
        if operation == "preview_retire" {
            return Ok(PublicationRequest::PreviewRetire {
                expected_revision,
                publication_version,
                acknowledge_self,
                key,
                confirmation,
            });
        }
        let (intent_digest, presentation) = presentation(fields)?;
        return Ok(PublicationRequest::Retire {
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
            confirmation,
            intent_digest,
            presentation,
        });
    }
    if operation == "preview_rollback" {
        return Ok(PublicationRequest::PreviewRollback {
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
        });
    }
    let (intent_digest, presentation) = presentation(fields)?;
    Ok(PublicationRequest::Rollback {
        expected_revision,
        publication_version,
        acknowledge_self,
        intent_digest,
        key,
        presentation,
    })
}

fn confirmation(fields: &mut Map) -> DecodeResult<String> {
    match take(fields, "confirmation", "/confirmation")? {
        Value::Str(value)
            if !value.is_empty() && value.len() <= 75 && !value.chars().any(char::is_control) =>
        {
            Ok(value)
        }
        _ => Err(invalid("/confirmation")),
    }
}

fn presentation(fields: &mut Map) -> DecodeResult<(String, Vec<u8>)> {
    let Value::Str(digest) = take(fields, "intent_digest", "/intent_digest")? else {
        return Err(invalid("/intent_digest"));
    };
    if !is_digest(&digest) {
        return Err(invalid("/intent_digest"));
    }
    let Value::Str(presentation) = take(fields, "presentation", "/presentation")? else {
        return Err(invalid("/presentation"));
    };
    if presentation.is_empty() || presentation.len() > MAX_PRESENTATION {
        return Err(invalid("/presentation"));
    }
    Ok((digest, presentation.into_bytes()))
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
    execute_v1_with_service(&Service::new(store, authority), credential, request)
}

/// Compatibility entry point for the existing publication client.
pub fn execute_v1_with_publication(
    service: &Service<'_>,
    credential: &[u8],
    request: &[u8],
) -> Vec<u8> {
    execute_v1_with_service(service, credential, request)
}

/// Shared byte boundary for the configured management application service.
pub fn execute_v1_with_service(
    service: &Service<'_>,
    credential: &[u8],
    request: &[u8],
) -> Vec<u8> {
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
            Self::Validation(value)
            | Self::Simulation(value)
            | Self::Comparison(value)
            | Self::Publication(value)
            | Self::Contract(value)
            | Self::Pack(value)
            | Self::Evaluator(value)
            | Self::Waiver(value) => value,
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
            "The specified policy violates a policy requirement.",
        ),
        Error::ActiveDependents => (
            "ACTIVE_DEPENDENTS",
            "The publication has active dependents.",
        ),
        Error::InvalidPack => (
            "INVALID_PACK",
            "The pack violates its verification or lifecycle requirements.",
        ),
        Error::PackConflict => ("PACK_CONFLICT", "The pack conflicts with an active pack."),
        Error::ProtectedContract => ("PROTECTED_CONTRACT", "Core contracts are immutable."),
        Error::InvalidContract => (
            "INVALID_CONTRACT",
            "The custom contract violates its profile or lifecycle.",
        ),
        Error::IncompatibleContract => (
            "INCOMPATIBLE_CONTRACT",
            "Compatibility does not permit the requested publication.",
        ),
        Error::Unsupported => (
            "UNSUPPORTED",
            "The operation is unsupported by this policy profile.",
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
