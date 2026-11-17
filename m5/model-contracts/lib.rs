//! M5b-05 dependency-free proposal contracts and deterministic fixture providers.

#![forbid(unsafe_code)]

/// The kind of work requested of a model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Task {
    Code,
    Research,
    Review,
}

/// Capability required to generate the requested proposal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability {
    Text,
    StructuredTools,
}

/// Data residency advertised by the fixture.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Residency {
    Local,
    HostedEu,
}

/// Requirements established by the caller before provider selection.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request<'a> {
    pub task: Task,
    pub prompt: &'a str,
    pub capability: Capability,
    pub allowed_residencies: &'a [Residency],
}

/// Agent-side model output. An effect requires separate broker authorization.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Proposal {
    Text(&'static str),
    Tool {
        name: &'static str,
        arguments: &'static str,
    },
}

/// The fixture provider failed to produce a result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderError {
    Unavailable,
    UnsupportedCapability,
}

/// A fixture adapter. An implementation supplies no policy or effect authority.
pub trait ModelAdapter {
    fn residency(&self) -> Residency;
    fn supports(&self, capability: Capability) -> bool;
    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError>;
}

/// Provider selection outcomes for this agent-side fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Run {
    ExternalHost,
    Proposed(Proposal),
    NoEligibleProvider,
    ProvidersUnavailable,
}

/// External hosts need no local model or provider credentials.
pub fn external_host() -> Run {
    Run::ExternalHost
}

/// Try eligible adapters in caller-supplied order. Recheck each fallback's requirements.
pub fn managed(request: &Request<'_>, adapters: &[&dyn ModelAdapter]) -> Run {
    let mut eligible = false;
    for adapter in adapters {
        if !request.allowed_residencies.contains(&adapter.residency())
            || !adapter.supports(request.capability)
        {
            continue;
        }
        eligible = true;
        if let Ok(proposal) = adapter.propose(request) {
            let shape_allowed = matches!(
                (request.capability, &proposal),
                (Capability::Text, Proposal::Text(_))
                    | (Capability::StructuredTools, Proposal::Tool { .. })
            );
            if shape_allowed {
                return Run::Proposed(proposal);
            }
        }
    }
    if eligible {
        Run::ProvidersUnavailable
    } else {
        Run::NoEligibleProvider
    }
}

/// The local fixture supports text only.
pub struct LocalText {
    pub offline: bool,
}

impl ModelAdapter for LocalText {
    fn residency(&self) -> Residency {
        Residency::Local
    }

    fn supports(&self, capability: Capability) -> bool {
        capability == Capability::Text
    }

    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError> {
        if !self.supports(request.capability) {
            return Err(ProviderError::UnsupportedCapability);
        }
        if self.offline {
            return Err(ProviderError::Unavailable);
        }
        let text = match request.task {
            Task::Code => "Draft a patch for review",
            Task::Research => "Collect sources for review",
            Task::Review => "Summarize the review findings",
        };
        Ok(Proposal::Text(text))
    }
}

/// The hosted fixture proposes structured tool calls only.
pub struct HostedTools {
    pub offline: bool,
}

impl ModelAdapter for HostedTools {
    fn residency(&self) -> Residency {
        Residency::HostedEu
    }

    fn supports(&self, capability: Capability) -> bool {
        capability == Capability::StructuredTools
    }

    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError> {
        if !self.supports(request.capability) {
            return Err(ProviderError::UnsupportedCapability);
        }
        if self.offline {
            return Err(ProviderError::Unavailable);
        }
        let (name, arguments) = match request.task {
            Task::Code => ("inspect_source", "src/lib.rs"),
            Task::Research => ("suggest_search", "documentation"),
            Task::Review => ("inspect_diff", "pending changes"),
        };
        Ok(Proposal::Tool { name, arguments })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coding_research_and_review_produce_inert_proposals() {
        let local = LocalText { offline: false };
        let hosted = HostedTools { offline: false };
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
            let request = Request {
                task,
                prompt: "synthetic fixture",
                capability: Capability::Text,
                allowed_residencies: &[Residency::Local, Residency::HostedEu],
            };
            assert_eq!(
                managed(&request, &[&hosted, &local]),
                Run::Proposed(Proposal::Text(text))
            );
            let tools_request = Request {
                capability: Capability::StructuredTools,
                ..request
            };
            assert_eq!(
                managed(&tools_request, &[&local, &hosted]),
                Run::Proposed(Proposal::Tool {
                    name: tool,
                    arguments
                })
            );
        }
    }

    #[test]
    fn fallback_respects_capability_and_residency_before_provider_calls() {
        struct Forbidden;
        impl ModelAdapter for Forbidden {
            fn residency(&self) -> Residency {
                Residency::HostedEu
            }
            fn supports(&self, _capability: Capability) -> bool {
                true
            }
            fn propose(&self, _request: &Request<'_>) -> Result<Proposal, ProviderError> {
                panic!("the forbidden provider must not receive the prompt")
            }
        }

        let offline = LocalText { offline: true };
        let online = LocalText { offline: false };
        let request = Request {
            task: Task::Review,
            prompt: "local-only fixture",
            capability: Capability::Text,
            allowed_residencies: &[Residency::Local],
        };
        assert_eq!(
            managed(&request, &[&offline, &Forbidden]),
            Run::ProvidersUnavailable
        );
        assert!(matches!(
            managed(&request, &[&offline, &Forbidden, &online]),
            Run::Proposed(_)
        ));
        assert_eq!(
            managed(
                &Request {
                    capability: Capability::StructuredTools,
                    ..request
                },
                &[&online, &Forbidden]
            ),
            Run::NoEligibleProvider
        );
    }

    #[test]
    fn mismatched_proposal_is_ignored_and_fallback_stays_eligible() {
        struct WrongShape;
        impl ModelAdapter for WrongShape {
            fn residency(&self) -> Residency {
                Residency::Local
            }
            fn supports(&self, capability: Capability) -> bool {
                capability == Capability::Text
            }
            fn propose(&self, _request: &Request<'_>) -> Result<Proposal, ProviderError> {
                Ok(Proposal::Tool {
                    name: "fixture",
                    arguments: "",
                })
            }
        }

        let request = Request {
            task: Task::Review,
            prompt: "local-only fixture",
            capability: Capability::Text,
            allowed_residencies: &[Residency::Local],
        };
        assert_eq!(managed(&request, &[&WrongShape]), Run::ProvidersUnavailable);
        assert_eq!(
            managed(&request, &[&WrongShape, &LocalText { offline: false }]),
            Run::Proposed(Proposal::Text("Summarize the review findings"))
        );
    }

    #[test]
    fn external_host_needs_no_model() {
        assert_eq!(external_host(), Run::ExternalHost);
    }
}
