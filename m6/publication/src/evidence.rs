//! Evidence for the isolated broker and host-contract test profiles.
use writ_kernel::{
    json::{self, Digest, Value},
    time::Timestamp,
};

use crate::{Audit, Error, Publication, Scope};

const PROFILE: &str = "synthetic-publication-lab/1";
const POINT: &str = "m6-publication-lab/inspect-broker";
const RULES: &str = "every_registered_rule_runs_through_the_selected_permit_consuming_effect";
const REPLAY: &str = "verified_restart_loads_the_exact_pair_and_replays_effect_once";
const EXPIRED: &str = "effect_rechecks_time_before_consuming_the_permit";
const APPROVAL: &str = "activation_waits_for_a_granted_effect_and_then_hard_deny_prevents_more_use";

/// An immutable test-profile registration supplied by the existing pinned trust guard.
#[derive(Clone)]
pub struct FixtureEvidence {
    profile: &'static str,
    point: Option<&'static str>,
    expires: &'static str,
    revoked: bool,
    positive: Vec<&'static str>,
    negative: Vec<&'static str>,
}

impl FixtureEvidence {
    /// Load the built-in registration, test references and validity window.
    pub fn registered() -> Self {
        Self {
            profile: PROFILE,
            point: Some(POINT),
            expires: "2027-11-19T00:00:00Z",
            revoked: false,
            positive: vec![RULES, REPLAY, APPROVAL],
            negative: vec![RULES, EXPIRED, APPROVAL],
        }
    }

    /// Construct a weakened registration for the trusted disposable fixture only.
    pub fn scenario(name: &str) -> Result<Option<Self>, Error> {
        let mut evidence = Self::registered();
        match name {
            "current" => (),
            "removed" => evidence.point = None,
            "stale" => evidence.expires = "2000-01-01T00:00:00Z",
            "mismatched" => evidence.profile = "other-test-profile",
            "unavailable" => return Ok(None),
            "revoked" => evidence.revoked = true,
            "missing-positive" => evidence.positive.clear(),
            "missing-negative" => evidence.negative.clear(),
            _ => return Err(Error::Format),
        };
        Ok(Some(evidence))
    }

    pub(crate) fn check(&self, scope: &Scope, now: Timestamp) -> Result<(), &'static str> {
        if self.revoked {
            return Err("EVIDENCE_REVOKED");
        }
        if self.point != Some(POINT) {
            return Err("ENFORCEMENT_POINT_MISSING");
        }
        if self.profile != PROFILE
            || (scope.tenant(), scope.project()) != ("m6-lab", "offline-proof")
        {
            return Err("PROFILE_MISMATCH");
        }
        if !Timestamp::parse_strict(self.expires).is_some_and(|end| now < end)
            || !Timestamp::parse_strict("2026-11-19T00:00:00Z").is_some_and(|start| start <= now)
        {
            return Err("STALE_EVIDENCE");
        }
        if ![RULES, REPLAY, APPROVAL]
            .iter()
            .all(|id| self.positive.contains(id))
        {
            return Err("POSITIVE_TEST_MISSING");
        }
        if ![RULES, EXPIRED, APPROVAL]
            .iter()
            .all(|id| self.negative.contains(id))
        {
            return Err("NEGATIVE_TEST_MISSING");
        }
        if !pins_current() {
            return Err("IMPLEMENTATION_MISMATCH");
        }
        if !passing_record(
            include_bytes!(concat!(env!("OUT_DIR"), "/mediation-result.json")),
            implementation(),
            PROFILE,
            &[RULES, REPLAY, APPROVAL, EXPIRED],
        ) {
            return Err("TEST_EVIDENCE_MISMATCH");
        }
        Ok(())
    }
}

fn passing_record(input: &[u8], identity: &str, profile: &str, required: &[&str]) -> bool {
    let Ok(record) = json::parse(input) else {
        return false;
    };
    let Some(passed) = record.get("passed").and_then(Value::as_arr) else {
        return false;
    };
    record.get("version") == Some(&json::s("writ-fixture-test-result/1"))
        && record.get("profile") == Some(&json::s(profile))
        && record.get("implementation_digest") == Some(&json::s(identity))
        && passed.len() == required.len()
        && required.iter().all(|id| passed.contains(&json::s(id)))
}

fn pins_current() -> bool {
    static VERIFIED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *VERIFIED.get_or_init(|| {
        Digest::of_bytes(COMPILED_SOURCE_IDS.as_bytes()).as_str() == implementation()
    })
}

fn strings(items: &[&str]) -> Value {
    Value::Arr(items.iter().map(|item| json::s(item)).collect())
}

struct Subject<'a> {
    id: &'a str,
    kind: &'static str,
    label: &'static str,
    profile: &'static str,
    implementation: Option<&'a str>,
    point: Option<&'a str>,
    positive: &'a [&'a str],
    negative: &'a [&'a str],
}

impl Subject<'_> {
    fn row(&self, scope: &Scope, policy: Option<&str>, reason: Option<&str>) -> Value {
        json::obj([
            (
                "label",
                json::s(if reason.is_some() {
                    "UNVERIFIED"
                } else {
                    self.label
                }),
            ),
            ("subject", json::s(self.id)),
            ("kind", json::s(self.kind)),
            ("profile", json::s(self.profile)),
            ("scope", scope.to_value()),
            ("policy", policy.map_or(Value::Null, json::s)),
            (
                "implementation",
                self.implementation.map_or(Value::Null, json::s),
            ),
            ("enforcement_point", self.point.map_or(Value::Null, json::s)),
            ("positive_tests", strings(self.positive)),
            ("negative_tests", strings(self.negative)),
            (
                "freshness",
                json::s(match reason {
                    Some("STALE_EVIDENCE" | "POLICY_EXPIRED") => "stale",
                    None => "current",
                    _ => "unknown",
                }),
            ),
            (
                "compatibility",
                json::s(match reason {
                    Some(
                        "PROFILE_MISMATCH"
                        | "IMPLEMENTATION_MISMATCH"
                        | "POLICY_MISMATCH"
                        | "TEST_EVIDENCE_MISMATCH"
                        | "COVERAGE_MISMATCH"
                        | "GUIDANCE_MISMATCH"
                        | "UNSUPPORTED_COVERAGE",
                    ) => "incompatible",
                    None => "compatible",
                    _ => "unknown",
                }),
            ),
            ("reasons", strings(&reason.into_iter().collect::<Vec<_>>())),
        ])
    }
}

pub(crate) fn rows(
    scope: &Scope,
    publication: &Publication,
    evidence: Option<&FixtureEvidence>,
    reason: Option<&str>,
) -> Result<Vec<Value>, Error> {
    let digest = publication.fingerprint()?;
    let draft = publication
        .prerequisites
        .get("draft")
        .ok_or(Error::Recovery)?;
    let rules = draft
        .get("companions")
        .and_then(Value::as_arr)
        .ok_or(Error::Recovery)?;
    let mut rows = Vec::new();
    let point = evidence.and_then(|record| record.point);
    let implementation = point.and_then(|_| (!implementation().is_empty()).then(implementation));
    let positive = evidence.map_or(&[][..], |e| e.positive.as_slice());
    let negative = evidence.map_or(&[][..], |e| e.negative.as_slice());
    let mediated = |id, kind, label| {
        Subject {
            id,
            kind,
            label,
            profile: PROFILE,
            implementation,
            point,
            positive,
            negative,
        }
        .row(scope, Some(digest.as_str()), reason)
    };
    for rule in rules {
        let id = rule
            .get("policy_id")
            .and_then(Value::as_str)
            .ok_or(Error::Recovery)?;
        let effect = rule.get("effect").and_then(Value::as_str);
        rows.push(mediated(
            id,
            "rule",
            if effect == Some("require_approval") {
                "APPROVAL REQUIRED"
            } else {
                "ENFORCED"
            },
        ));
    }
    for (id, kind) in [
        ("synthetic.inspect", "capability"),
        ("m6-publication-lab/broker", "integration_path"),
        ("synthetic.inspect/controlled-effect", "effect"),
    ] {
        rows.push(mediated(id, kind, "ENFORCED"));
    }
    let host_profile = "fixture-host/fixture-1.0/schema-fixture";
    let host = json::parse(include_bytes!(concat!(
        env!("OUT_DIR"),
        "/host-coverage.json"
    )))
    .unwrap_or(Value::Null);
    let records = host.get("rows").and_then(Value::as_arr).unwrap_or_default();
    for (test_id, expected_id) in [
        (
            "synthetic-prompt-01",
            "writ-host-coverage/1/synthetic-prompt-01/Write/workspace-file/fs.write",
        ),
        (
            "synthetic-subagent-01",
            "writ-host-coverage/1/synthetic-subagent-01/subagent_start",
        ),
    ] {
        let mut matches = records
            .iter()
            .filter(|record| record.get("test_id").and_then(Value::as_str) == Some(test_id));
        let row = matches
            .next()
            .filter(|_| matches.next().is_none())
            .unwrap_or(&Value::Null);
        let text = |name| row.get(name).and_then(Value::as_str);
        let variant = row.get("tool_variant");
        let cites = |name| {
            variant
                .and_then(|variant| variant.get(name))
                .and_then(Value::as_str)
        };
        let observes = row.get("can_observe").and_then(Value::as_bool);
        let prevents = row.get("can_prevent").and_then(Value::as_bool);
        let (id, label, point, positive, negative) = match (
            text("test_id"),
            text("event"),
            text("interceptor"),
            text("capability"),
            cites("name"),
            cites("variant"),
            observes,
            prevents,
        ) {
            (
                Some("synthetic-prompt-01"),
                Some("permission_prompt"),
                Some("permission_prompt"),
                Some("fs.write"),
                Some("Write"),
                Some("workspace-file"),
                Some(true),
                Some(false),
            ) => (
                "writ-host-coverage/1/synthetic-prompt-01/Write/workspace-file/fs.write",
                "HOOK-INTEGRATED (ADVISORY)",
                Some("permission_prompt"),
                "both_advisory_paths_project_paired_fixture_inputs",
                "embedded_advisory_claims_do_not_change_projection",
            ),
            (
                Some("synthetic-subagent-01"),
                Some("subagent_start"),
                Some("none"),
                None,
                None,
                None,
                Some(false),
                Some(false),
            ) if row.get("capability") == Some(&Value::Null) && variant == Some(&Value::Null) => (
                "writ-host-coverage/1/synthetic-subagent-01/subagent_start",
                "UNMEDIATED",
                None,
                "test_drafts_and_examples",
                "test_coverage_for_no_interceptor_cannot_claim_observation_or_prevention",
            ),
            _ => (expected_id, "UNVERIFIED", None, "", ""),
        };
        let reason = if !host_pin_current() {
            Some("COVERAGE_MISMATCH")
        } else if label == "UNVERIFIED" {
            Some("UNSUPPORTED_COVERAGE")
        } else {
            None
        };
        let positives = [positive];
        let negatives = [negative];
        rows.push(
            Subject {
                id,
                kind: "integration_path",
                label,
                profile: host_profile,
                implementation: (!HOST_SEAL.trim().is_empty()).then_some(HOST_SEAL.trim()),
                point,
                positive: if positive.is_empty() { &[] } else { &positives },
                negative: if negative.is_empty() { &[] } else { &negatives },
            }
            .row(scope, None, reason),
        );
    }
    rows.push(
        Subject {
            id: "DOC-ONBOARD-001",
            kind: "rule",
            label: "ADVISORY",
            profile: "fixture-guidance/1",
            implementation: Some(GUIDANCE_SEAL),
            point: None,
            positive: &[],
            negative: &[],
        }
        .row(
            scope,
            None,
            (!guidance_pin_current()).then_some("GUIDANCE_MISMATCH"),
        ),
    );
    rows.push(
        Subject {
            id: "live-host",
            kind: "integration_path",
            label: "UNVERIFIED",
            profile: "live-host",
            implementation: None,
            point: None,
            positive: &[],
            negative: &[],
        }
        .row(scope, None, Some("LIVE_EVIDENCE_UNAVAILABLE")),
    );
    Ok(rows)
}

pub(crate) fn history(
    events: &[Audit],
    next: Option<i64>,
    selected: Option<(&Audit, bool)>,
) -> Value {
    json::obj([
        (
            "selected",
            selected.map_or(Value::Null, |(event, verified)| {
                json::obj([
                    ("revision", Value::Int(event.revision)),
                    ("publication_version", Value::Int(event.publication_version)),
                    ("audit_id", Value::Int(event.sequence)),
                    ("verified", Value::Bool(verified)),
                ])
            }),
        ),
        (
            "events",
            Value::Arr(
                events
                    .iter()
                    .map(|event| {
                        json::obj([
                            ("audit_id", Value::Int(event.sequence)),
                            ("revision", Value::Int(event.revision)),
                            ("operation", json::s(event.operation.as_str())),
                            ("publication_version", Value::Int(event.publication_version)),
                            ("publication_digest", json::s(&event.publication_digest)),
                            ("intent_digest", json::s(&event.intent_digest)),
                            ("human", json::s(&event.human)),
                            ("at", json::s(&event.at.to_string())),
                            ("context", Value::Null),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("next", next.map_or(Value::Null, Value::Int)),
    ])
}

const IMPLEMENTATION_SEAL: &str = include_str!(concat!(env!("OUT_DIR"), "/mediation.seal"));
fn implementation() -> &'static str {
    IMPLEMENTATION_SEAL.trim()
}
const COMPILED_SOURCE_IDS: &str = include_str!(concat!(env!("OUT_DIR"), "/mediation-source-ids"));
const HOST_SEAL: &str = include_str!(concat!(env!("OUT_DIR"), "/host-coverage.seal"));
const GUIDANCE_SEAL: &str =
    "sha256:e35f8c517224e2047d88a971699405e025f31238d9f2348c33835b1bda5354f2";
fn host_pin_current() -> bool {
    Digest::of_bytes(include_bytes!(concat!(env!("OUT_DIR"), "/host-source-ids"))).as_str()
        == HOST_SEAL.trim()
        && passing_record(
            include_bytes!(concat!(env!("OUT_DIR"), "/host-coverage-result.json")),
            HOST_SEAL.trim(),
            "fixture-host/fixture-1.0/schema-fixture",
            &[
                "both_advisory_paths_project_paired_fixture_inputs",
                "embedded_advisory_claims_do_not_change_projection",
                "test_drafts_and_examples",
                "test_coverage_for_no_interceptor_cannot_claim_observation_or_prevention",
            ],
        )
}
fn guidance_pin_current() -> bool {
    Digest::of_bytes(include_bytes!(concat!(env!("OUT_DIR"), "/advisory-entry"))).as_str()
        == GUIDANCE_SEAL
}
