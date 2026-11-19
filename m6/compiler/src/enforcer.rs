use writ_kernel::contracts::StateRevision;
use writ_kernel::decision::{decide, Policy};
use writ_kernel::json::{self, Digest, Value};
use writ_kernel::policy::LoadedBundle;
use writ_kernel::time::Timestamp;

pub(crate) struct Enforcer {
    pub(crate) id: &'static str,
    pub(crate) identity: fn() -> Option<Digest>,
    pub(crate) run: fn(&Value, &LoadedBundle) -> Option<Value>,
}

pub(crate) const REGISTRY: [Enforcer; 1] = [Enforcer {
    id: "m6-kernel-inspect/1",
    identity,
    run: inspect,
}];

fn inspect(request: &Value, bundle: &LoadedBundle) -> Option<Value> {
    Some(
        decide(
            request,
            Policy::Loaded(bundle),
            StateRevision::new(1)?,
            Timestamp::parse_strict("2026-11-19T00:00:00Z")?,
        )
        .to_value(),
    )
}

fn identity() -> Option<Digest> {
    let sources = json::obj([
        ("enforcer", json::s(include_str!("enforcer.rs"))),
        ("lock", json::s(include_str!("../Cargo.lock"))),
        (
            "toolchain",
            json::s(include_str!("../../../rust-toolchain.toml")),
        ),
        (
            "kernel_manifest",
            json::s(include_str!("../../../kernel/Cargo.toml")),
        ),
        (
            "kernel_acceptance",
            json::s(include_str!("../../../kernel/src/acceptance.rs")),
        ),
        (
            "kernel_commands",
            json::s(include_str!("../../../kernel/src/commands.rs")),
        ),
        (
            "kernel_contracts",
            json::s(include_str!("../../../kernel/src/contracts.rs")),
        ),
        (
            "kernel_decision",
            json::s(include_str!("../../../kernel/src/decision.rs")),
        ),
        (
            "kernel_ids",
            json::s(include_str!("../../../kernel/src/ids.rs")),
        ),
        (
            "kernel_json",
            json::s(include_str!("../../../kernel/src/json.rs")),
        ),
        (
            "kernel_lib",
            json::s(include_str!("../../../kernel/src/lib.rs")),
        ),
        (
            "kernel_policy",
            json::s(include_str!("../../../kernel/src/policy.rs")),
        ),
        (
            "kernel_refusal",
            json::s(include_str!("../../../kernel/src/refusal.rs")),
        ),
        (
            "kernel_time",
            json::s(include_str!("../../../kernel/src/time.rs")),
        ),
        (
            "kernel_transition",
            json::s(include_str!("../../../kernel/src/transition.rs")),
        ),
    ]);
    json::digest(&sources).ok()
}
