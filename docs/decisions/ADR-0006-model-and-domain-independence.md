# ADR-0006 — Model- and domain-agnostic Writ V2

**Status:** ACCEPTED TARGET ARCHITECTURE · 2026-11-05

## Context

Writ V1 is coupled to coding and Claude Code. A generic policy engine alone would not deliver research, independent review, or other models if prompts, workflow phases, artifacts and evaluators were still hardcoded for files, commits or tests.

## Decision

1. The kernel and broker must never import a model-provider SDK.
2. Domain-specific stages, artifact types, rubrics, memory classes and tools live in versioned Capability Packs, not in the kernel.
3. HostAdapter supports externally managed agents without requiring Writ to call a model.
4. An optional AgentRunner/ModelAdapter layer supports Writ-managed tasks and provider capability negotiation.
5. The first three reference packs are Software Engineering, Research and Review/Audit; none is mandatory to install core Writ.
6. Model-produced text, task plans and pack manifests never grant capabilities or override signed authorization policy.
7. M0 and the Rust/Go bake-off must include noncoding TaskSpec and evaluation cases.

## Alternatives rejected

Claude-only agent host; model requests inside kernel; one untyped generic tool string for all domains; forks per use case; always-on remote multi-agent coordination. Each undermines portability, security or local-first operation.

## Consequences

Add draft ModelCapabilities, ModelRequest, ModelEvent, CapabilityPack, EvaluationRecord contracts; planned TaskSpec/Artifact/Evidence contracts, evaluator ports and pack registry. Check cross-provider compatibility through stub fixtures. No universal support claim until specific host/provider adapter is implemented and verified.

See [model](../architecture/model-agnostic-runtime.md), [packs](../architecture/capability-packs.md), [runner](../architecture/agent-orchestration.md), [M0](../implementation/M0-START-HERE.md).
