use crate::compile::ACTION;
use writ_kernel::contracts::{Effect, Statement};
use writ_kernel::decision::{in_scope, is_canonical_resource_id};
use writ_kernel::ids::is_id;
use writ_kernel::json::{self, Value};

pub(crate) fn supported_prefix(prefix: &str, tenant: &str) -> bool {
    let Some(path) = prefix.strip_prefix("synthetic:") else {
        return false;
    };
    let Some(project) = path.split('/').nth(1) else {
        return false;
    };
    is_id(tenant)
        && is_id(project)
        && is_canonical_resource_id(prefix)
        && in_scope(prefix, tenant, project)
}

struct Selector<'a> {
    tenant: &'a str,
    prefix: &'a str,
}

impl<'a> Selector<'a> {
    fn read(statement: &'a Statement) -> Option<Self> {
        let [tenant] = statement.tenants.as_deref()? else {
            return None;
        };
        let [prefix] = statement.resource_prefixes.as_deref()? else {
            return None;
        };
        if statement.action_kinds != [ACTION]
            || statement.principal_roles.is_some()
            || statement.from_states.is_some()
            || statement.to_states.is_some()
            || !supported_prefix(prefix, tenant)
        {
            return None;
        }
        Some(Self { tenant, prefix })
    }

    fn intersection(&self, other: &Self) -> Option<&'a str> {
        if self.tenant != other.tenant {
            return None;
        }
        // Kernel resource prefixes are raw starts_with predicates. The longer
        // prefix is itself a valid witness when one prefix contains the other.
        if self.prefix.starts_with(other.prefix) {
            Some(self.prefix)
        } else if other.prefix.starts_with(self.prefix) {
            Some(other.prefix)
        } else {
            None
        }
    }
}

fn effect_name(effect: Effect) -> &'static str {
    match effect {
        Effect::Forbid => "forbid",
        Effect::RequireApproval => "require_approval",
        Effect::Permit => "permit",
    }
}

pub(crate) fn analyze(statements: &[Statement]) -> Result<Vec<Value>, Vec<String>> {
    let mut selectors = Vec::with_capacity(statements.len());
    let mut diagnostics = Vec::new();
    for statement in statements {
        match Selector::read(statement) {
            Some(selector) => selectors.push((statement, selector)),
            None => diagnostics.push(format!("UNSUPPORTED_SELECTOR: {}", statement.statement_id)),
        }
    }
    if !diagnostics.is_empty() {
        diagnostics.sort();
        diagnostics.dedup();
        return Err(diagnostics);
    }

    selectors.sort_by(|(left, _), (right, _)| left.statement_id.cmp(&right.statement_id));
    let mut overlaps = Vec::new();
    for (index, (left, left_selector)) in selectors.iter().enumerate() {
        for (right, right_selector) in &selectors[index + 1..] {
            let Some(resource) = left_selector.intersection(right_selector) else {
                continue;
            };
            if left.effect == right.effect && left.reason_code != right.reason_code {
                diagnostics.push(format!(
                    "CONFLICT_SAME_TIER_REASON: {} {}",
                    left.statement_id, right.statement_id
                ));
            } else {
                overlaps.push(json::obj([
                    (
                        "statement_ids",
                        Value::Arr(vec![
                            json::s(&left.statement_id),
                            json::s(&right.statement_id),
                        ]),
                    ),
                    (
                        "effects",
                        Value::Arr(vec![
                            json::s(effect_name(left.effect)),
                            json::s(effect_name(right.effect)),
                        ]),
                    ),
                    ("witness_resource", json::s(resource)),
                ]));
            }
        }
    }
    if diagnostics.is_empty() {
        Ok(overlaps)
    } else {
        diagnostics.sort();
        Err(diagnostics)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn statement(id: &str, tenant: &str, project_path: &str, effect: Effect) -> Statement {
        Statement {
            statement_id: id.into(),
            effect,
            tenants: Some(vec![tenant.into()]),
            principal_roles: None,
            action_kinds: vec![ACTION.into()],
            resource_prefixes: Some(vec![format!("synthetic:{tenant}/{project_path}")]),
            reason_code: "SYNTHETIC_RULE".into(),
            from_states: None,
            to_states: None,
        }
    }

    #[test]
    fn byte_prefix_intersection_retains_equal_reason_and_cross_tier_pairs() {
        let broad = statement("b", "alice", "project/alpha", Effect::Permit);
        let narrow = statement("a", "alice", "project/alphabet", Effect::Permit);
        let deny = statement("c", "alice", "project/alphabet/locked", Effect::Forbid);
        let left = analyze(&[broad.clone(), deny.clone(), narrow.clone()]);
        let right = analyze(&[deny, narrow, broad]);
        assert_eq!(left, right);
        assert_eq!(
            left,
            Ok(vec![
                json::obj([
                    (
                        "statement_ids",
                        Value::Arr(vec![json::s("a"), json::s("b")])
                    ),
                    (
                        "effects",
                        Value::Arr(vec![json::s("permit"), json::s("permit")])
                    ),
                    (
                        "witness_resource",
                        json::s("synthetic:alice/project/alphabet")
                    ),
                ]),
                json::obj([
                    (
                        "statement_ids",
                        Value::Arr(vec![json::s("a"), json::s("c")])
                    ),
                    (
                        "effects",
                        Value::Arr(vec![json::s("permit"), json::s("forbid")])
                    ),
                    (
                        "witness_resource",
                        json::s("synthetic:alice/project/alphabet/locked")
                    ),
                ]),
                json::obj([
                    (
                        "statement_ids",
                        Value::Arr(vec![json::s("b"), json::s("c")])
                    ),
                    (
                        "effects",
                        Value::Arr(vec![json::s("permit"), json::s("forbid")])
                    ),
                    (
                        "witness_resource",
                        json::s("synthetic:alice/project/alphabet/locked")
                    ),
                ]),
            ])
        );
    }

    #[test]
    fn conflicting_reasons_require_intersection() {
        let broad = statement("b", "alice", "project/alpha", Effect::RequireApproval);
        let mut narrow = statement("a", "alice", "project/alphabet", Effect::RequireApproval);
        narrow.reason_code = "OTHER_RULE".into();
        let expected = Err(vec!["CONFLICT_SAME_TIER_REASON: a b".into()]);
        assert_eq!(analyze(&[broad.clone(), narrow.clone()]), expected);
        assert_eq!(analyze(&[narrow, broad.clone()]), expected);

        let mut other_tenant = statement("c", "bob", "project/alpha", Effect::RequireApproval);
        let mut other_path = statement("d", "alice", "project/beta", Effect::RequireApproval);
        let mut other_project = statement("e", "alice", "other/alpha", Effect::RequireApproval);
        for rule in [&mut other_tenant, &mut other_path, &mut other_project] {
            rule.reason_code = "OTHER_RULE".into();
        }
        assert_eq!(
            analyze(&[broad, other_tenant, other_path, other_project]),
            Ok(vec![])
        );
    }

    #[test]
    fn refuses_unknown_or_non_singleton_selectors() {
        let baseline = statement("a", "alice", "project/alpha", Effect::Forbid);
        let expected = Err(vec!["UNSUPPORTED_SELECTOR: a".into()]);
        for unsupported in [
            Statement {
                tenants: None,
                ..baseline.clone()
            },
            Statement {
                tenants: Some(vec!["alice".into(), "bob".into()]),
                ..baseline.clone()
            },
            Statement {
                resource_prefixes: None,
                ..baseline.clone()
            },
            Statement {
                resource_prefixes: Some(vec!["synthetic:bob/project/alpha".into()]),
                ..baseline.clone()
            },
            Statement {
                resource_prefixes: Some(vec!["synthetic:alice/project/../file".into()]),
                ..baseline.clone()
            },
            Statement {
                action_kinds: vec![ACTION.into(), "synthetic.other".into()],
                ..baseline.clone()
            },
            Statement {
                principal_roles: Some(vec![]),
                ..baseline.clone()
            },
            Statement {
                from_states: Some(vec!["draft".into()]),
                ..baseline.clone()
            },
            Statement {
                to_states: Some(vec!["approved".into()]),
                ..baseline
            },
        ] {
            assert_eq!(analyze(&[unsupported]), expected);
        }
    }
}
