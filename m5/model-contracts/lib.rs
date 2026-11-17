//! M5b-05 dependency-free proposal contracts and deterministic fixture providers.

#![forbid(unsafe_code)]

use std::collections::HashSet;

/// The kind of work requested of a model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Task {
    Code,
    Research,
    Review,
}

/// Capability required by the agent-side task.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability {
    Text,
    StructuredTools,
}

/// Data residency of a configured provider binding.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Residency {
    Local,
    HostedEu,
}

/// Where an approved adapter would send a request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Egress<'a> {
    None,
    Endpoint(&'a str),
}

/// Agent-side model input without credentials, policy or approval material.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request<'a> {
    pub task: Task,
    pub prompt: &'a str,
    pub capability: Capability,
}

/// Service-owned tenant and project binding supplied by the local test interface.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Scope<'a> {
    pub tenant: &'a str,
    pub project: &'a str,
}

/// Locally configured provider eligibility for a tenant and project.
pub struct RoutingPolicy<'a> {
    pub scope: Scope<'a>,
    pub allowed_providers: &'a [&'a str],
    pub allowed_residencies: &'a [Residency],
    pub approved_endpoints: &'a [&'a str],
}

/// Locally configured adapter binding. Routing facts come from this binding.
pub struct ProviderBinding<'a> {
    pub id: &'a str,
    pub residency: Residency,
    pub egress: Egress<'a>,
    pub capabilities: &'a [Capability],
    pub adapter: &'a dyn ModelAdapter,
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

/// A fixture adapter. The adapter receives only the request it was selected for.
pub trait ModelAdapter {
    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError>;
}

/// Agent-side runner outcomes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Run {
    ExternalHost,
    Proposed(Proposal),
    RoutingPolicyUnavailable,
    RoutingConfigurationInvalid,
    NoEligibleProvider,
    ProvidersUnavailable,
}

/// Fixture runner with no policy evaluation or effect API.
pub struct AgentRunner<'a> {
    pub policies: &'a [RoutingPolicy<'a>],
    pub providers: &'a [ProviderBinding<'a>],
}

impl AgentRunner<'_> {
    /// The external-host path requires no model binding or managed prompt.
    pub fn external_host() -> Run {
        Run::ExternalHost
    }

    /// Apply the bound routing policy before sharing the prompt with each candidate.
    pub fn managed(&self, scope: Scope<'_>, request: &Request<'_>) -> Run {
        let mut matching = self.policies.iter().filter(|policy| policy.scope == scope);
        let Some(policy) = matching.next() else {
            return Run::RoutingPolicyUnavailable;
        };
        if matching.next().is_some() {
            return Run::RoutingPolicyUnavailable;
        }

        let mut provider_ids = HashSet::new();
        if self
            .providers
            .iter()
            .any(|binding| binding.id.is_empty() || !provider_ids.insert(binding.id))
        {
            return Run::RoutingConfigurationInvalid;
        }

        let mut eligible = false;
        for binding in self.providers {
            let egress_allowed = match (binding.residency, binding.egress) {
                (Residency::Local, Egress::None) => true,
                (Residency::HostedEu, Egress::Endpoint(endpoint)) => {
                    !endpoint.is_empty() && policy.approved_endpoints.contains(&endpoint)
                }
                _ => false,
            };
            if !policy.allowed_providers.contains(&binding.id)
                || !policy.allowed_residencies.contains(&binding.residency)
                || !binding.capabilities.contains(&request.capability)
                || !egress_allowed
            {
                continue;
            }
            eligible = true;
            if let Ok(proposal) = binding.adapter.propose(request) {
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
}

/// The local fixture supports text only.
pub struct LocalText {
    pub offline: bool,
}

impl ModelAdapter for LocalText {
    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError> {
        if request.capability != Capability::Text {
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
    fn propose(&self, request: &Request<'_>) -> Result<Proposal, ProviderError> {
        if request.capability != Capability::StructuredTools {
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
mod tests;
