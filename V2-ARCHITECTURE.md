# Writ V2 — Architecture overview

> **Status: architecture and implementation specification only.** This branch is not a functioning V2 runtime. The existing V1 source tree is retained as an isolated reference until a replacement is implemented, verified, and explicitly cut over.

Writ V2 is a **model-agnostic, domain-agnostic intelligence and execution platform** for AI-assisted work. Claude Code is the first external host integration, not the platform's boundary. Research, independent review, business operations and software engineering are distinct Capability Packs. Writ can serve an existing agent or, optionally, manage its own model-driven task loop. It combines **just-in-time retrieval**, **independently enforced permissions and human approvals**, and **persistent, provenance-aware memory**. Models advise and propose; the trusted execution boundary authorizes and performs side effects.

## Start here

- [Documentation index](docs/README.md)
- [Full implementation plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md) — roadmap, detailed deliverables, dependencies, exit criteria, testing and cutover
- [Target architecture](docs/architecture/architecture.md)
- [Execution and state](docs/architecture/execution-and-state.md)
- [Authorization and approvals](docs/architecture/authorization-and-approvals.md)
- [JIT retrieval and memory](docs/architecture/retrieval-and-memory.md)
- [Model independence and integrations](docs/architecture/model-agnostic-runtime.md)
- [Domain Capability Packs](docs/architecture/capability-packs.md)
- [Agent orchestration and evaluation](docs/architecture/agent-orchestration.md)
- [M0 start checklist](docs/implementation/M0-START-HERE.md)
- [Security invariants](docs/security/invariants.md)
- [V1 audit and disposition](docs/audit/V1-ARCHITECTURE-BASELINE.md)
- [HTML architecture site](https://infinri.github.io/Writ/v2/docs/architecture/index.html)

## Core principles

1. **One execution authority.** The capability broker mediates privileged actions; hook scripts do not duplicate policies.
2. **Model output is untrusted.** Retrieved content, transcripts, tool arguments and agent assertions never grant authority.
3. **Policy has a stable machine-readable contract.** Every enforced control has an owner, version, evidence, tests and explicit fallback behavior.
4. **Approvals are real capabilities.** Human approvals bind an authenticated principal, exact operation, resource, policy version, and expiry, and cannot be replayed.
5. **Separate fast local decisions from distributed workflows.** An ordinary tool decision must not depend on a remote workflow orchestrator or a graph query.
6. **Graph intelligence is not authorization.** Neo4j holds relationships, knowledge and memory; transactional authorization and execution records live in operational storage.
7. **Ports, adapters and bounded modules.** Provider-specific hooks stay at the edge. Enterprise services are optional until validated by workloads.
8. **Model/provider neutrality.** External agent hosts need not send inference requests through Writ; optional ModelAdapters serve Writ-managed agents.
9. **Domain neutrality.** Coding, research and review are modular task packs, not hardcoded kernel behaviors.
10. **Explicit evaluation.** Model conclusions are hypotheses until validated against typed evidence and rubrics.
11. **No self-editing security posture.** Adaptive retrieval and planning may change recommendations, never silently rewrite enforcement rules.

## What is committed versus undecided

**Architecturally committed:** external-agent and optional managed-agent modes; ModelAdapter and CapabilityPack boundaries; domain-neutral task/evidence/artifact contracts; a small deterministic execution core; explicit authorization; host-backed effect mediation; typed action and event contracts; Neo4j for provenance and graph intelligence; durable operational state; traceable policy/rule registry; portable host adapters; local-first functionality; tenant-aware authorization.

**Pending benchmark/ADR:** Rust versus Go as the primary kernel language. Rust/Cedar/Wasmtime and Go/Cedar-Go/wazero are candidate implementations. Neither is adopted before the same parity, latency, memory and operability suite runs.

**Deferred:** Temporal/Restate, Redis, WASM plugins and Kubernetes. These are optional modules or enterprise deployment choices—not default core dependencies.

## Important branch warning

The legacy Python, Bash and Claude Code hook files are **still present solely as reference material**, so existing V1 code is not suddenly compliant with these V2 documents. The old 150-file documentation tree has been replaced for this branch; the historical source and documentation remain in the main branch's Git history. Do not deploy this branch as a new version of the plugin or interpret specification examples as shipped functionality. The V1 release remains on main.

## Working agreement

Changes must map to a milestone and at least one testable invariant in the [implementation plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md). At every milestone provide a reproducible build, adversarial tests, benchmark outputs and a recorded go/no-go decision. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Live architecture website

[Open the V2 architecture site](https://infinri.github.io/Writ/v2/docs/architecture/index.html) · [View the source HTML](docs/architecture/index.html). The live link requires the GitHub Pages deployment workflow and GitHub Pages to be configured for **GitHub Actions**. If deployment is blocked by repository Pages settings, the source still exists in this branch.
