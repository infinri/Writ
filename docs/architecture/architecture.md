# Writ V2 — system architecture

**Status:** DECIDED target boundaries; underlying implementations not shipped. [Master plan](../implementation/MASTER-IMPLEMENTATION-PLAN.md).

## Architecture in one sentence

A small deterministic **decision kernel** authorizes normalized actions; a **capability broker** is the actual executor; retrieval and graph intelligence supply evidence but never mint authority; host adapters supply authenticated identity and transport.

~~~mermaid
flowchart LR
  A[Agent / host] --> H[Host adapter]
  H --> N[Action normalizer]
  N --> K[Kernel / state machine]
  P[Versioned policy] --> K
  S[(Operational state)] <--> K
  K --> B[Capability broker]
  B --> E[Effect adapter]
  B --> R[(Effect receipts)]
  H --> J[JIT intelligence]
  J --> X[Lexical & vector indexes]
  J --> G[(Neo4j knowledge graph)]
  J --> M[Recall]
  Q[Optional workflow coordinator] --> H
~~~

## Trust boundaries

**T1 Host→Adapter:** untrusted agent tool requests become validated, canonical, identity-bound actions. Host-provided identity must be cryptographically or OS/transport verified where possible. Caller-supplied string IDs are not authentication.

**T2 Adapter→Kernel:** only normalized versioned ActionRequest; invalid/missing critical fields are INVALID/UNAVAILABLE. No ability to smuggle dynamic policies in request payload.

**T3 Kernel→Policy:** signed or pinned immutable bundle. Unknown policy version, failed signature, incomplete load and conflicting rules on privileged paths reject, never silently allow.

**T4 Kernel→Broker:** a permit is scoped to exact action/resource/content digest and tenant. Broker independently rechecks freshness; it cannot be driven via alternate route bypassing the kernel.

**T5 Broker→Effect:** only declared adapters with explicit capability lists, sandbox/OS permissions and receipts. No universal shell inspection claim.

**T6 Intelligence→Model:** retrieved guidance is fenced/cited untrusted context; it never enters the authority plane. Graph links produce evidence, not permission.

**T7 Tenant boundaries:** all state, memory, graph reads, policy resolution, audit and quotas are scoped through authenticated tenant/principal context.

## Logical modules

| Module | Owns | Must not own |
|---|---|---|
| Contracts | Wire schema, canonicalization, versioning | Runtime identity |
| Core | State transitions, decision semantics | Network and filesystem effects |
| Policy | Authorization bundle and rule provenance | LLM instructions as permissions |
| Broker | Controlled effects, receipt, idempotency | Policy authorship |
| Host adapters | Host event shape, identity binding | Additional private rule copies |
| Retrieval | Ranking, abstention, token budgets | Authorization |
| Memory | Evidence history, retention, provenance | Approval secrets |
| Graph | Knowledge and relationship queries | Workflow lock/counter |
| Operations | Durable events, approvals, receipts, revision | Rule-ranking heuristics |
| Coordinator | Long-running scheduling and retry | Bypass of broker |
| Observability | Traces, diagnostics, tamper-evident audits | Ability to turn a deny into allow |

## Performance boundary

The permission hot path should need only: validated request, verified identity, in-memory/pinned policy and locally available operational state. Graph, vector retrieval, remote LLM and remote durable workflow services are never unconditional prerequisites to deciding an ordinary privileged tool action. Failures are explicit; context generation can degrade without changing a policy verdict.

## Modes

Conversation, investigation, review, plan and implementation are **workflow state**. They do not change fundamental tenant or organization permissions. Any user-configured workflows are orthogonal to the security decision.

## Extension points

HostAdapter, PolicyProvider, ActionExecutor, GraphPort, RetrievalPort, MemoryPort, EventStore and WorkflowCoordinator. Each has a versioned contract; test doubles prove interchangeability. See [extensions](extensibility.md) and [repository layout](repository-layout.md).

## Model and domain independence

The external agent host (Claude Code initially, others later) may own the entire model and agent loop: Writ connects through HostAdapter and never needs direct provider API access in this mode. Optional Writ-managed AgentRunner connects to ModelAdapter for inference and CapabilityPacks for domain TaskSpecs, evaluation rubrics and artifact/evidence schemas; tool effects still require the same kernel/broker path. No coding stage, PR field, provider SDK or research rubric belongs inside the deterministic core. See [Model Runtime](model-agnostic-runtime.md), [Capability Packs](capability-packs.md), [AgentRunner](agent-orchestration.md), and [ADR-0006](../decisions/ADR-0006-model-and-domain-independence.md).
