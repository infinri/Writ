use writ_kernel::{commands, json};
use writ_model_contracts::{
    AgentRunner, Capability, Egress, HostedTools, Proposal, ProviderBinding, Request, Residency,
    RoutingPolicy, Run, Scope, Task,
};

#[test]
fn real_kernel_decisions_survive_provider_outage_and_model_proposals() {
    const FIXTURES: &str = include_str!("../../m1/fixtures/sets/golden-decisions.jsonl");
    const ENDPOINT: &str = "https://provider.example.invalid";
    let scope = Scope {
        tenant: "fixture-tenant",
        project: "fixture-project",
    };
    let policies = [RoutingPolicy {
        scope,
        allowed_providers: &["hosted"],
        allowed_residencies: &[Residency::HostedEu],
        approved_endpoints: &[ENDPOINT],
    }];
    let request = Request {
        task: Task::Review,
        prompt: "synthetic fixture",
        capability: Capability::StructuredTools,
    };
    let online = HostedTools { offline: false };
    let offline = HostedTools { offline: true };
    let binding = ProviderBinding {
        id: "hosted",
        residency: Residency::HostedEu,
        egress: Egress::Endpoint(ENDPOINT),
        capabilities: &[Capability::StructuredTools],
        adapter: &online,
    };
    let unavailable = AgentRunner {
        policies: &policies,
        providers: &[ProviderBinding {
            adapter: &offline,
            ..binding
        }],
    };
    let available = AgentRunner {
        policies: &policies,
        providers: &[binding],
    };
    for (id, status) in [
        ("workspace-file-write", "allow"),
        ("secrets-write-forbidden", "deny"),
        ("publish-requires-approval", "require_approval"),
        ("no-policy-loaded", "unavailable"),
    ] {
        let fixture = FIXTURES
            .lines()
            .map(|line| json::parse(line.as_bytes()).unwrap())
            .find(|value| value.get("id").and_then(json::Value::as_str) == Some(id))
            .unwrap();
        let input = fixture.get("input").unwrap().to_json_text();
        let baseline = commands::decide(input.as_bytes());
        assert_eq!(
            json::parse(baseline.as_bytes())
                .unwrap()
                .get("status")
                .and_then(json::Value::as_str),
            Some(status),
            "{id}"
        );
        assert_eq!(
            unavailable.managed(scope, &request),
            Run::ProvidersUnavailable
        );
        assert_eq!(commands::decide(input.as_bytes()), baseline, "{id}: outage");
        assert_eq!(
            available.managed(scope, &request),
            Run::Proposed(Proposal::Tool {
                name: "inspect_diff",
                arguments: "pending changes"
            })
        );
        assert_eq!(
            commands::decide(input.as_bytes()),
            baseline,
            "{id}: proposal"
        );
    }
}
