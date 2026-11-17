use super::*;

const SCOPE: Scope<'static> = Scope {
    tenant: "fixture-tenant",
    project: "fixture-project",
};
const ENDPOINT: &str = "https://provider.example.invalid";
const REQUEST: Request<'_> = Request {
    task: Task::Review,
    prompt: "synthetic fixture",
    capability: Capability::Text,
};

fn policy<'a>(allowed_providers: &'a [&'static str]) -> RoutingPolicy<'a> {
    RoutingPolicy {
        scope: SCOPE,
        allowed_providers,
        allowed_residencies: &[Residency::Local, Residency::HostedEu],
        approved_endpoints: &[ENDPOINT],
    }
}

fn local<'a>(id: &'a str, adapter: &'a dyn ModelAdapter) -> ProviderBinding<'a> {
    ProviderBinding {
        id,
        residency: Residency::Local,
        egress: Egress::None,
        capabilities: &[Capability::Text],
        adapter,
    }
}

#[test]
fn coding_research_and_review_produce_inert_proposals() {
    let providers = [
        local("local", &LocalText { offline: false }),
        ProviderBinding {
            id: "hosted",
            residency: Residency::HostedEu,
            egress: Egress::Endpoint(ENDPOINT),
            capabilities: &[Capability::StructuredTools],
            adapter: &HostedTools { offline: false },
        },
    ];
    let runner = AgentRunner {
        policies: &[policy(&["local", "hosted"])],
        providers: &providers,
    };
    for (task, text, tool, arguments) in [
        (
            Task::Code,
            "Draft a patch for review",
            "inspect_source",
            "src/lib.rs",
        ),
        (
            Task::Research,
            "Collect sources for review",
            "suggest_search",
            "documentation",
        ),
        (
            Task::Review,
            "Summarize the review findings",
            "inspect_diff",
            "pending changes",
        ),
    ] {
        let request = Request { task, ..REQUEST };
        assert_eq!(
            runner.managed(SCOPE, &request),
            Run::Proposed(Proposal::Text(text))
        );
        let tools_request = Request {
            capability: Capability::StructuredTools,
            ..request
        };
        assert_eq!(
            runner.managed(SCOPE, &tools_request),
            Run::Proposed(Proposal::Tool {
                name: tool,
                arguments
            })
        );
    }
}

struct MustNotReceivePrompt;

impl ModelAdapter for MustNotReceivePrompt {
    fn propose(&self, _request: &Request<'_>) -> Result<Proposal, ProviderError> {
        panic!("an ineligible provider must not receive the prompt")
    }
}

#[test]
fn each_ineligible_binding_is_excluded_before_provider_call() {
    let offline = LocalText { offline: true };
    let online = LocalText { offline: false };
    let providers = [
        local("offline", &offline),
        local("unlisted", &MustNotReceivePrompt),
        ProviderBinding {
            residency: Residency::HostedEu,
            ..local("wrong-region", &MustNotReceivePrompt)
        },
        ProviderBinding {
            egress: Egress::Endpoint("https://unapproved.example.invalid"),
            ..local("wrong-endpoint", &MustNotReceivePrompt)
        },
        ProviderBinding {
            capabilities: &[Capability::StructuredTools],
            ..local("wrong-capability", &MustNotReceivePrompt)
        },
        local("backup", &online),
    ];
    let rules = [RoutingPolicy {
        allowed_residencies: &[Residency::Local],
        ..policy(&[
            "offline",
            "wrong-region",
            "wrong-endpoint",
            "wrong-capability",
            "backup",
        ])
    }];
    let runner = AgentRunner {
        policies: &rules,
        providers: &providers[..providers.len() - 1],
    };
    assert_eq!(runner.managed(SCOPE, &REQUEST), Run::ProvidersUnavailable);
    assert_eq!(
        (AgentRunner {
            providers: &providers,
            ..runner
        })
        .managed(SCOPE, &REQUEST),
        Run::Proposed(Proposal::Text("Summarize the review findings"))
    );
    assert_eq!(
        (AgentRunner {
            policies: &rules,
            providers: &providers[1..5]
        })
        .managed(SCOPE, &REQUEST),
        Run::NoEligibleProvider
    );
}

#[test]
fn absent_or_ambiguous_scope_cannot_send_a_prompt() {
    let providers = [local("provider", &MustNotReceivePrompt)];
    let runner = AgentRunner {
        policies: &[policy(&["provider"])],
        providers: &providers,
    };
    assert_eq!(
        runner.managed(
            Scope {
                tenant: "other",
                ..SCOPE
            },
            &REQUEST
        ),
        Run::RoutingPolicyUnavailable
    );
    assert_eq!(
        runner.managed(
            Scope {
                project: "other",
                ..SCOPE
            },
            &REQUEST
        ),
        Run::RoutingPolicyUnavailable
    );
    let runner = AgentRunner {
        policies: &[policy(&["provider"]), policy(&["provider"])],
        providers: &providers,
    };
    assert_eq!(
        runner.managed(SCOPE, &REQUEST),
        Run::RoutingPolicyUnavailable
    );
}

#[test]
fn mismatched_proposal_is_ignored_and_fallback_stays_eligible() {
    struct WrongShape;

    impl ModelAdapter for WrongShape {
        fn propose(&self, _request: &Request<'_>) -> Result<Proposal, ProviderError> {
            Ok(Proposal::Tool {
                name: "fixture",
                arguments: "",
            })
        }
    }

    let online = LocalText { offline: false };
    let providers = [local("wrong-shape", &WrongShape), local("backup", &online)];
    let rules = [policy(&["wrong-shape", "backup"])];
    let runner = AgentRunner {
        policies: &rules,
        providers: &providers[..1],
    };
    assert_eq!(runner.managed(SCOPE, &REQUEST), Run::ProvidersUnavailable);
    assert_eq!(
        (AgentRunner {
            providers: &providers,
            ..runner
        })
        .managed(SCOPE, &REQUEST),
        Run::Proposed(Proposal::Text("Summarize the review findings"))
    );
}

#[test]
fn external_host_needs_no_model() {
    assert_eq!(AgentRunner::external_host(), Run::ExternalHost);
}

#[test]
fn hosted_provider_requires_the_configured_endpoint_to_be_approved() {
    let rules = [policy(&["hosted"])];
    for egress in [
        Egress::None,
        Egress::Endpoint("https://unapproved.example.invalid"),
    ] {
        let providers = [ProviderBinding {
            residency: Residency::HostedEu,
            egress,
            ..local("hosted", &MustNotReceivePrompt)
        }];
        let runner = AgentRunner {
            policies: &rules,
            providers: &providers,
        };
        assert_eq!(runner.managed(SCOPE, &REQUEST), Run::NoEligibleProvider);
    }
}

#[test]
fn duplicate_provider_id_is_rejected_before_any_adapter_call() {
    let providers = [
        local("provider", &MustNotReceivePrompt),
        ProviderBinding {
            residency: Residency::HostedEu,
            egress: Egress::Endpoint(ENDPOINT),
            ..local("provider", &MustNotReceivePrompt)
        },
    ];
    let runner = AgentRunner {
        policies: &[policy(&["provider"])],
        providers: &providers,
    };
    assert_eq!(
        runner.managed(SCOPE, &REQUEST),
        Run::RoutingConfigurationInvalid
    );
}
