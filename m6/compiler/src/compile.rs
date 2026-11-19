use std::collections::{BTreeMap, BTreeSet};

use writ_kernel::contracts::{Statement, CONTRACT_VERSION};
use writ_kernel::ids::{is_id, is_reason_code};
use writ_kernel::json::{self, Map, Value};
use writ_kernel::policy::load_bundle;

use crate::conflict::supported_prefix;
use crate::Diagnostic;

pub(crate) const ACTION: &str = "synthetic.inspect";

const MANIFEST_SCHEMA: &[u8] =
    include_bytes!("../../../docs/contracts/policy-manifest.schema.json");

type Key = (String, String);
type EntryResult<T> = Result<T, &'static str>;

pub(crate) struct Draft {
    pub(crate) bundle: Value,
    pub(crate) rules: Vec<Rule>,
}

pub(crate) struct Rule {
    pub(crate) manifest: Value,
    pub(crate) statement: Statement,
    pub(crate) priority: i64,
    pub(crate) binding: String,
    pub(crate) vectors: Vec<String>,
}

struct Manifest {
    value: Value,
    tenant: String,
    action_kinds: Value,
    priority: i64,
    binding: String,
    vectors: Vec<String>,
}

struct Companion {
    prefix: String,
    effect: String,
    reason: String,
}

fn nonempty(map: &Map, field: &str) -> Option<String> {
    map.get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn key(map: &Map) -> Option<Key> {
    let policy_id = nonempty(map, "policy_id")?;
    let suffix = policy_id.strip_prefix("m6.synthetic.")?;
    if suffix.is_empty()
        || !suffix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
    {
        return None;
    }
    Some((policy_id, nonempty(map, "version")?))
}

fn manifest(value: &Value, required: &[Value], properties: &Map) -> EntryResult<(Key, Manifest)> {
    const SUPPORTED_FIELDS: [&str; 15] = [
        "manifest_version",
        "policy_id",
        "version",
        "owner",
        "tenant_scope",
        "enforcement_class",
        "action_kinds",
        "priority",
        "failure_posture",
        "evaluator_binding",
        "test_vectors",
        "bundle_digest",
        "publisher_signature",
        "expires_at",
        "description",
    ];
    let map = value.as_obj().ok_or("MANIFEST_SHAPE_INVALID")?;
    if map
        .keys()
        .any(|field| !properties.contains_key(field) || !SUPPORTED_FIELDS.contains(&field.as_str()))
        || !required
            .iter()
            .all(|field| field.as_str().is_some_and(|field| map.contains_key(field)))
    {
        return Err("MANIFEST_FIELDS_INVALID");
    }
    if value.get("manifest_version").and_then(Value::as_str) != Some(CONTRACT_VERSION)
        || value.get("enforcement_class").and_then(Value::as_str) != Some("authorization")
        || value.get("failure_posture").and_then(Value::as_str) != Some("deny")
    {
        return Err("MANIFEST_SEMANTICS_UNSUPPORTED");
    }
    if ["bundle_digest", "publisher_signature", "expires_at"]
        .iter()
        .any(|field| !matches!(map.get(*field), None | Some(Value::Null)))
    {
        return Err("MANIFEST_PUBLICATION_FIELDS_UNSUPPORTED");
    }
    if !matches!(
        map.get("description"),
        None | Some(Value::Null | Value::Str(_))
    ) || nonempty(map, "owner").is_none()
    {
        return Err("MANIFEST_METADATA_INVALID");
    }

    let key = key(map).ok_or("MANIFEST_IDENTITY_UNSUPPORTED")?;
    let tenant = nonempty(map, "tenant_scope")
        .filter(|tenant| is_id(tenant))
        .ok_or("MANIFEST_TENANT_UNSUPPORTED")?;
    let action_kinds = map
        .get("action_kinds")
        .filter(|kinds| matches!(kinds.as_arr(), Some([kind]) if kind.as_str() == Some(ACTION)))
        .ok_or("MANIFEST_ACTION_KINDS_UNSUPPORTED")?
        .clone();
    let priority = value
        .get("priority")
        .and_then(Value::as_int)
        .ok_or("MANIFEST_PRIORITY_INVALID")?;
    let binding = nonempty(map, "evaluator_binding").ok_or("MANIFEST_BINDING_INVALID")?;
    let mut vectors = value
        .get("test_vectors")
        .and_then(Value::as_arr)
        .filter(|vectors| !vectors.is_empty())
        .and_then(|vectors| {
            vectors
                .iter()
                .map(|vector| {
                    vector
                        .as_str()
                        .filter(|id| !id.is_empty())
                        .map(str::to_owned)
                })
                .collect::<Option<Vec<_>>>()
        })
        .ok_or("MANIFEST_TEST_VECTORS_INVALID")?;
    vectors.sort();
    if vectors.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err("MANIFEST_TEST_VECTORS_DUPLICATE");
    }

    let mut normalized = map.clone();
    normalized.insert(
        "test_vectors".into(),
        Value::Arr(vectors.iter().map(|id| json::s(id)).collect()),
    );
    Ok((
        key,
        Manifest {
            value: Value::Obj(normalized),
            tenant,
            action_kinds,
            priority,
            binding,
            vectors,
        },
    ))
}

fn companion(value: &Value) -> EntryResult<(Key, Companion)> {
    const FIELDS: [&str; 6] = [
        "entry_version",
        "policy_id",
        "version",
        "effect",
        "reason_code",
        "resource_prefix",
    ];
    let map = value.as_obj().ok_or("COMPANION_SHAPE_INVALID")?;
    if map.len() != FIELDS.len() || !FIELDS.iter().all(|field| map.contains_key(*field)) {
        return Err("COMPANION_FIELDS_INVALID");
    }
    if value.get("entry_version").and_then(Value::as_str) != Some("m6-authz-entry/1") {
        return Err("COMPANION_VERSION_UNSUPPORTED");
    }

    let key = key(map).ok_or("COMPANION_IDENTITY_UNSUPPORTED")?;
    let effect = nonempty(map, "effect")
        .filter(|effect| matches!(effect.as_str(), "forbid" | "require_approval" | "permit"))
        .ok_or("COMPANION_EFFECT_UNSUPPORTED")?;
    let reason = nonempty(map, "reason_code")
        .filter(|reason| is_reason_code(reason))
        .ok_or("COMPANION_REASON_INVALID")?;
    let prefix = nonempty(map, "resource_prefix").ok_or("COMPANION_PREFIX_INVALID")?;
    Ok((
        key,
        Companion {
            effect,
            reason,
            prefix,
        },
    ))
}

fn inputs(input: &Value) -> EntryResult<(&[Value], &[Value])> {
    let map = input.as_obj().ok_or("INPUT_SHAPE_INVALID")?;
    if map.len() != 2 {
        return Err("INPUT_FIELDS_INVALID");
    }
    let manifests = map
        .get("manifests")
        .and_then(Value::as_arr)
        .filter(|entries| !entries.is_empty())
        .ok_or("INPUT_MANIFESTS_INVALID")?;
    let companions = map
        .get("companions")
        .and_then(Value::as_arr)
        .filter(|entries| !entries.is_empty())
        .ok_or("INPUT_COMPANIONS_INVALID")?;
    Ok((manifests, companions))
}

fn entry_pointer(group: &str, index: usize, code: &str) -> String {
    let path = format!("/{group}/{index}");
    let field = match code {
        "MANIFEST_TENANT_UNSUPPORTED" => "tenant_scope",
        "MANIFEST_ACTION_KINDS_UNSUPPORTED" => "action_kinds",
        "MANIFEST_PRIORITY_INVALID" => "priority",
        "MANIFEST_BINDING_INVALID" => "evaluator_binding",
        "MANIFEST_TEST_VECTORS_INVALID" | "MANIFEST_TEST_VECTORS_DUPLICATE" => "test_vectors",
        "COMPANION_VERSION_UNSUPPORTED" => "entry_version",
        "COMPANION_EFFECT_UNSUPPORTED" => "effect",
        "COMPANION_REASON_INVALID" => "reason_code",
        "COMPANION_PREFIX_INVALID" => "resource_prefix",
        _ => return path,
    };
    format!("{path}/{field}")
}

fn entries<T>(
    values: &[Value],
    parse: impl Fn(&Value) -> EntryResult<(Key, T)>,
    duplicate_code: &'static str,
    group: &str,
    errors: &mut Vec<Diagnostic>,
) -> BTreeMap<Key, (usize, T)> {
    let mut by_key = BTreeMap::new();
    for (index, value) in values.iter().enumerate() {
        match parse(value) {
            Ok((key, row)) => {
                if by_key.insert(key, (index, row)).is_some() {
                    errors.push(Diagnostic::new(
                        duplicate_code,
                        entry_pointer(group, index, duplicate_code),
                    ));
                }
            }
            Err(code) => {
                errors.push(Diagnostic::new(code, entry_pointer(group, index, code)));
            }
        }
    }
    by_key
}

fn statement_id((policy_id, version): &Key) -> EntryResult<String> {
    let identity = json::obj([
        ("policy_id", json::s(policy_id)),
        ("version", json::s(version)),
    ]);
    let digest = json::digest(&identity).map_err(|_| "STATEMENT_ID_INVALID")?;
    Ok(format!("m6-{}", &digest.as_str()[7..39]))
}

pub(crate) fn compile_detailed(input: &[u8]) -> Result<Draft, Vec<Diagnostic>> {
    let at_root = |code| vec![Diagnostic::new(code, "")];
    let document = json::parse(input).map_err(|error| at_root(error.code()))?;
    let (manifests, companions) = inputs(&document).map_err(|code| {
        let pointer = match code {
            "INPUT_MANIFESTS_INVALID" => "/manifests",
            "INPUT_COMPANIONS_INVALID" => "/companions",
            _ => "",
        };
        vec![Diagnostic::new(code, pointer)]
    })?;
    let schema = json::parse(MANIFEST_SCHEMA).map_err(|_| at_root("MANIFEST_SCHEMA_INVALID"))?;
    let Some(required) = schema.get("required").and_then(Value::as_arr) else {
        return Err(at_root("MANIFEST_SCHEMA_INVALID"));
    };
    let Some(properties) = schema.get("properties").and_then(Value::as_obj) else {
        return Err(at_root("MANIFEST_SCHEMA_INVALID"));
    };

    let mut errors = Vec::new();
    let manifests = entries(
        manifests,
        |value| manifest(value, required, properties),
        "MANIFEST_KEY_DUPLICATE",
        "manifests",
        &mut errors,
    );
    let mut companions = entries(
        companions,
        companion,
        "COMPANION_KEY_DUPLICATE_OR_CONFLICTING",
        "companions",
        &mut errors,
    );
    if !errors.is_empty() {
        return Err(errors);
    }

    let mut compiled = BTreeMap::new();
    for (key, (manifest_index, manifest)) in manifests {
        let Some((companion_index, companion)) = companions.remove(&key) else {
            errors.push(Diagnostic::new(
                "COMPANION_MISSING",
                format!("/manifests/{manifest_index}"),
            ));
            continue;
        };
        if !supported_prefix(&companion.prefix, &manifest.tenant) {
            errors.push(Diagnostic::new(
                "COMPANION_PREFIX_UNSUPPORTED",
                format!("/companions/{companion_index}/resource_prefix"),
            ));
            continue;
        }
        let statement_id = statement_id(&key).map_err(at_root)?;
        let statement = json::obj([
            ("statement_id", json::s(&statement_id)),
            ("effect", json::s(&companion.effect)),
            ("tenants", Value::Arr(vec![json::s(&manifest.tenant)])),
            ("action_kinds", manifest.action_kinds.clone()),
            (
                "resource_prefixes",
                Value::Arr(vec![json::s(&companion.prefix)]),
            ),
            ("reason_code", json::s(&companion.reason)),
        ]);
        if compiled
            .insert(statement_id, (manifest, statement))
            .is_some()
        {
            errors.push(Diagnostic::new(
                "STATEMENT_ID_COLLISION",
                format!("/manifests/{manifest_index}"),
            ));
        }
    }
    for (index, _) in companions.into_values() {
        errors.push(Diagnostic::new(
            "COMPANION_WITHOUT_MANIFEST",
            format!("/companions/{index}"),
        ));
    }
    if !errors.is_empty() {
        return Err(errors);
    }

    let bundle = json::obj([
        ("contract_version", json::s(CONTRACT_VERSION)),
        ("bundle_id", json::s("m6-synthetic")),
        ("version", json::s("1")),
        ("publisher", json::s("m6-offline")),
        ("known_capabilities", Value::Arr(vec![json::s(ACTION)])),
        (
            "trusted_principal_issuers",
            Value::Arr(vec![json::s("m6-synthetic")]),
        ),
        ("publisher_signature", Value::Null),
        (
            "statements",
            Value::Arr(compiled.values().map(|(_, value)| value.clone()).collect()),
        ),
    ]);
    let loaded = load_bundle(&bundle).map_err(|_| at_root("KERNEL_BUNDLE_REJECTED"))?;
    let rules = loaded
        .bundle
        .statements
        .into_iter()
        .map(|statement| {
            let (manifest, _) = compiled
                .remove(&statement.statement_id)
                .ok_or("COMPILED_RULE_MISSING")?;
            Ok(Rule {
                manifest: manifest.value,
                statement,
                priority: manifest.priority,
                binding: manifest.binding,
                vectors: manifest.vectors,
            })
        })
        .collect::<EntryResult<Vec<_>>>()
        .map_err(at_root)?;
    Ok(Draft { bundle, rules })
}

pub(crate) fn compile(input: &[u8]) -> Result<Draft, Vec<String>> {
    compile_detailed(input).map_err(|diagnostics| {
        diagnostics
            .into_iter()
            .map(|issue| issue.code)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    const MANIFEST: &str = r#"{
        "manifest_version":"2.0",
        "policy_id":"m6.synthetic.test",
        "version":"1",
        "owner":"m6",
        "tenant_scope":"tenant",
        "enforcement_class":"authorization",
        "action_kinds":["synthetic.inspect"],
        "priority":99,
        "failure_posture":"deny",
        "evaluator_binding":"example",
        "test_vectors":["positive","negative"]
    }"#;

    const COMPANION: &str = r#"{
        "entry_version":"m6-authz-entry/1",
        "policy_id":"m6.synthetic.test",
        "version":"1",
        "effect":"permit",
        "reason_code":"SYNTHETIC_PERMIT",
        "resource_prefix":"synthetic:tenant/project/resource"
    }"#;

    fn input(manifests: &str, companions: &str) -> String {
        format!(r#"{{"manifests":[{manifests}],"companions":[{companions}]}}"#)
    }

    #[test]
    fn exact_pair_and_metadata_priority() {
        let first = compile(input(MANIFEST, COMPANION).as_bytes()).unwrap();
        let changed = MANIFEST
            .replace("99", "-1")
            .replace(r#""positive","negative""#, r#""negative","positive""#);
        let second = compile(input(&changed, COMPANION).as_bytes()).unwrap();
        assert_eq!(first.bundle, second.bundle);
        assert_eq!(first.rules[0].statement, second.rules[0].statement);
        assert_eq!(first.rules[0].priority, 99);
        assert_eq!(second.rules[0].priority, -1);
        assert_eq!(first.rules[0].vectors, second.rules[0].vectors);
        assert_eq!(first.rules[0].vectors, ["negative", "positive"]);

        let different_version = COMPANION.replace(r#""version":"1""#, r#""version":"2""#);
        let errors = compile(input(MANIFEST, &different_version).as_bytes())
            .err()
            .unwrap();
        assert_eq!(errors, ["COMPANION_MISSING", "COMPANION_WITHOUT_MANIFEST"]);
    }

    #[test]
    fn deterministic_duplicate_and_unsupported_diagnostics() {
        let bad = MANIFEST.replace(r#""priority":99"#, r#""priority":99,"roles":["admin"]"#);
        let changed_companion = COMPANION.replace("SYNTHETIC_PERMIT", "DIFFERENT");
        let inputs = [
            input(
                &format!("{MANIFEST},{MANIFEST},{bad}"),
                &format!("{COMPANION},{changed_companion}"),
            ),
            input(
                &format!("{bad},{MANIFEST},{MANIFEST}"),
                &format!("{changed_companion},{COMPANION}"),
            ),
        ];
        for input in inputs {
            let errors = compile(input.as_bytes()).err().unwrap();
            assert_eq!(
                errors,
                [
                    "COMPANION_KEY_DUPLICATE_OR_CONFLICTING",
                    "MANIFEST_FIELDS_INVALID",
                    "MANIFEST_KEY_DUPLICATE"
                ]
            );
        }
    }

    #[test]
    fn refuse_unsupported_semantics() {
        for (manifest, companion, code) in [
            (
                MANIFEST.replace("authorization", "advisory"),
                COMPANION.into(),
                "MANIFEST_SEMANTICS_UNSUPPORTED",
            ),
            (
                MANIFEST.replace("synthetic.inspect", "workflow.transition"),
                COMPANION.into(),
                "MANIFEST_ACTION_KINDS_UNSUPPORTED",
            ),
            (
                MANIFEST.replace(
                    r#""priority":99"#,
                    r#""priority":99,"publisher_signature":"signed""#,
                ),
                COMPANION.into(),
                "MANIFEST_PUBLICATION_FIELDS_UNSUPPORTED",
            ),
            (
                MANIFEST.into(),
                COMPANION.replace("synthetic:tenant", "synthetic:other"),
                "COMPANION_PREFIX_UNSUPPORTED",
            ),
            (
                MANIFEST.into(),
                COMPANION.replace(
                    r#""effect":"permit""#,
                    r#""effect":"permit","principal_roles":["admin"]"#,
                ),
                "COMPANION_FIELDS_INVALID",
            ),
        ] {
            let errors = compile(input(&manifest, &companion).as_bytes())
                .err()
                .unwrap();
            assert_eq!(errors, [code]);
        }
    }
}
