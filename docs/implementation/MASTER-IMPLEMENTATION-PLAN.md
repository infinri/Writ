# Writ V2 — master implementation specification

**Document:** V2-IMP-001 · **Status:** approved architectural direction, implementation pending · **Version:** 0.2 · **Date:** 2026-11-05

## 0. Executive decision

Rebuild Writ as a **model-agnostic, domain-agnostic intelligence and execution platform**: a small trusted kernel, with replaceable policy, retrieval, memory, graph and host interfaces. Two first-class operating modes are **external agent integration** (e.g. Claude Code, without Writ invoking a model) and **optional Writ-managed agent execution** (AgentRunner plus ModelAdapter). Software engineering is the initial reference Capability Pack, alongside noncode research and general document review. No model API, software-specific artifact, coding phase or domain-specific evaluator belongs inside the kernel. Do **not** translate the legacy Python/Bash application wholesale. Its tests and domain data are evidence to mine selectively, not architecture to imitate.

Three product capabilities survive:

1. **JIT knowledge**: provide the smallest relevant, scoped, provenance-bearing set of instructions and evidence near a decision.
2. **Enforcements / acceptance gates**: allow humans and organizations to define actual execution boundaries; separate hard denial, approval workflow, advisory instruction and unverified outcomes.
3. **Recall / persistent memory**: preserve warranted decisions, constraints, evidence and lessons with identity, consent, retention, provenance and project/tenant isolation.

V2 must be useful as a fast local tool without Neo4j availability on every action, yet able to expand into enterprise deployments with isolated tenants and coordinated workflows.

## 1. Desired properties and non-goals

### Product requirements

| ID | Requirement | Acceptance evidence |
|---|---|---|
| P-01 | Host-agnostic action normalization | Same action fixture produces equivalent verdict from 2 independent adapters |
| P-02 | Single privileged execution boundary | All supported privileged effects require a valid broker authorization record |
| P-03 | Human, not model, grants approval | Forged transcript or model message never creates a valid approval |
| P-04 | Consistent policy outcomes | Hard deny never becomes ask/allow through retry counters |
| P-05 | Deterministic state transitions | Replaying ordered events yields same state and decisions |
| P-06 | Reliable offline local use | Static policy and cached state can be evaluated without remote services |
| P-07 | JIT knowledge and abstention | Relevant rules/evidence ranked; low-confidence queries abstain |
| P-08 | Durable, scoped memory | A project cannot retrieve a different tenant's memory |
| P-09 | Auditable effects | Every attempted privileged effect has decision, policy version and effect receipt |
| P-10 | Extensibility | Adapters and analyzers replaceable without linking to kernel internals |
| P-11 | Enterprise hardening | Crash/retry tests show no unauthorized or duplicate externally visible effects |
| P-12 | Human override with bounds | Authorized approvals are exact, single use, expiry bound and recorded |
| P-13 | Model neutrality | Two stub model providers satisfy shared event/structured-output contracts; no model API imported into kernel |
| P-14 | External host independence | Host-managed agent works without a direct Writ model request |
| P-15 | Domain neutrality | Code-review, noncode research and document review fit one TaskSpec and artifact/evidence contracts |
| P-16 | Versioned Capability Packs | Pack manifests verify schemas/capability needs and do not grant authority |
| P-17 | Evaluator neutrality | Rubrics and evidence validation are domain-pack interfaces; model claims never count as independent verification |
| P-18 | Provider capability negotiation | Unsupported features/fallbacks are explicit and honor residency/privacy/cost requirements |
| P-19 | Managed agent separation | Optional AgentRunner can plan/stream tools without directly executing effects or weakening policy |
| P-20 | Writ Control Center: unified management and administration without routine direct JSON, YAML, script or Neo4j edits | Tier B requirements in [control-center](../control-center/README.md) met with CC-SEC and CC-UX tests at the M9 gate |
| P-21 | Management plane outside the decision path | CC-SEC-01: management service absent or overloaded, decisions and kernel p95 unchanged (INV-27) |
| P-22 | Every important action explainable | "Why did this happen?" for every decision status and effect state from recorded records only (CC-G06, CC-SEC-11, CC-UX-09) |
| P-23 | Administrative changes authenticated, authorized, audited and versioned; no bypass of mandatory invariants | CC-SEC-02, 04, 08, 12, 13 (INV-28..INV-30) |
| P-25 | Acceptance Gate Integrity: no protected workflow transition without kernel authorization and a current, bound AcceptanceRecord from trusted evaluators | AG test plan in [acceptance gate integrity](../architecture/acceptance-gate-integrity.md#11-test-plan-proposed) (INV-32..INV-39, proposed) |
| P-26 | No self-certification: agents, models and packs cannot produce passing mandatory criteria, signoffs or acceptance records | AG-NEG-03..06, AG-ADV-09 |
| P-27 | Honest modes: Advisory Integration never presented as Governed Execution | INV-39; Control Center labels |
| P-24 | Headless operation and CLI parity | Kernel, broker and adapters run without the UI; essential operations available in CLI and API (CC-G13, CC-G14) |

### Performance and operability objectives

Initial **targets, not measured facts**: local no-I/O decision p95 below 5 ms and p99 below 15 ms on target reference hardware; adapter-to-decision p95 below 30 ms for warmed local state; bounded memory per session; cold-start under 500 ms for minimal kernel binary; zero graph or LLM calls on normal deterministic permission checks. Baseline and calibrate these at M0. Retrieval, graph queries, network calls, and tool execution each have separate SLIs. Benchmark at 1, 10, 100 and 1,000 concurrent simulated sessions, then test real enterprise workloads. Never claim a global end-to-end latency SLA solely from hot-cache microbenchmarks.

### Explicit non-goals for first release

Not an OS-level sandbox; not a mandatory all-purpose autonomous agent (an optional managed runner may be added); not a graph database rewrite; not automatic escalation from an advisory rule to a denial; not support for every conceivable shell command; not a distributed workflow engine on each local hook; not silently improving security by instructing the LLM. Privileged operations outside the host-controlled path must be labeled **unmediated** unless a verified sandbox intercepts them.

## 2. Trust and ownership model

**Trusted components:** decision kernel, verified policy bundle loader, broker, approval ledger, host-authenticated identity boundary, optional process/container sandbox. **Untrusted data:** tool input, agent output, prompt text, transcripts, retrieved instructions, internet documents, imported memories and graph values.

**Authority hierarchy:** security invariants > organization policy > tenant/project policy > human approvals applicable under policy > workflow preferences > retrieved guidance. Lower layers never override stronger prohibitions. A model cannot edit this hierarchy.

**Owners:** kernel decides state and policy resolution; broker alone triggers governed effects; operational store owns approval/effect transactions; Neo4j owns knowledge edges; retrieval owns ranking only; host adapter owns transport and authenticated identity binding; workflow layer schedules durable jobs but never skips broker checks.

## 3. Decisions and open selections

| Component | Decision | Reason / exit gate |
|---|---|---|
| Runtime language | **OPEN** Rust vs Go | Run identical security, startup, memory, concurrency and operational workloads in M1 |
| Policy authorization | **DECIDED** Cedar-like scoped authorization via versioned bundles | Never accept model-supplied permissions; test deny precedence and unknown behavior |
| Additional expression language | **DEFERRED** CEL | Only if validated nonauthorization use cases warrant another DSL |
| Graph intelligence | **DECIDED** Neo4j behind explicit GraphPort | Graph is not in synchronous permission hot path |
| Operational state | **PROVISIONAL** PostgreSQL in enterprise; SQLite for local offline | Verify atomic approval consume, effect idempotency and migration behavior |
| Retrieval implementation | **OPEN** retain/port/adapt V1 ONNX-HNSW-Tantivy | Compare quality and memory before replacement |
| Module isolation | **DEFERRED** WASM | Introduce after stable plugin API and capability limits |
| Distributed workflows | **DEFERRED** Temporal or Restate | Needed for long-running distributed jobs, not local decisions |
| Redis/queue | **DEFERRED** | Only if workload evidence requires |
| API transport | **PROVISIONAL** local UDS/named pipe + typed RPC; HTTP gateway optional | Require explicit auth and local peer validation |
| Observability | **DECIDED** OpenTelemetry traces + append-only audit contracts | Separate telemetry (best effort) from authorization receipts (durable) |
| Tenancy | **DECIDED** explicit tenant_id/principal/project; deny-by-default | No ambient global project namespace |
| Model integrations | **DECIDED** separate ModelAdapter (managed mode) from HostAdapter (external mode) | Capability discovery and contract tests, never assume universal compatibility |
| Domain modules | **DECIDED** versioned Capability Packs | Coding, research and review fixtures; pack cannot publish policy or grant tools |
| AgentRunner | **PROVISIONAL** local optional task runner | Not required for external Claude/Codex/Gemini host integrations |
| Evaluation | **DECIDED** typed artifacts, source evidence and versioned rubrics | Mechanical/evidence/model-assisted/human classes with explicit limitations |
| Management plane | **DECIDED** separate Control Center management service with versioned authenticated APIs, outside the decision path ([ADR-0007](../decisions/ADR-0007-control-center-management-plane.md)) | CC-SEC-01; agent principals refused; registry enforces mandatory invariants |
| Management service language and UI technology | **OPEN** | Service language after ADR-0002; UI technology by ADR at the start of M9 |

## 4. Target boundaries

~~~mermaid
flowchart TB
  Host[External agent: Claude Code / Codex / other host] --> Adapter[Host adapter: verify identity, normalize]
  Model[Claude / GPT / Gemini / local model] --> ModelAdapter[ModelAdapter: capabilities and streaming]
  ModelAdapter --> Runner[Optional AgentRunner]
  Runner --> Adapter
  Pack[Capability Pack: tasks / artifacts / evaluators] --> Runner
  Pack --> Intelligence
  Adapter --> Kernel[Kernel: state, policy, decision]
  Kernel --> Policy[Versioned policy bundle]
  Kernel --> Approval[Approval service / operational ledger]
  Kernel --> Broker[Capability broker]
  Broker --> Effect[Governed effect]
  Broker --> Receipt[Effect receipt / audit]
  Adapter --> Intelligence[Intelligence gateway]
  Intelligence --> Retrieval[JIT index + embeddings]
  Intelligence --> Graph[Neo4j graph port]
  Intelligence --> Memory[Recall & provenance]
  Coordinator[Optional durable coordinator] --> Adapter
~~~

The broker **revalidates** the permit/approval at use time against target/action/digest/version, not only when the model requests an operation. Policy evaluation is pure; side effects occur only through an explicit execution method.

## 5. Repository blueprint

See [repository layout](../architecture/repository-layout.md). Target is a single repository with independent buildable packages: kernel, contracts, policy, broker, adapters, retrieval, memory, graph, persistence, CLI, gateway and test harnesses. Default local installation starts only kernel/broker/host adapter plus embedded policy and state. Writ-managed inference uses an optional AgentRunner and ModelAdapter. Domain logic lives in versioned Capability Packs, not core. Enterprise composition adds graph, Postgres, remote coordination and optional extension runtime.

## 6. Domain and wire contracts

The normalized **ActionRequest** carries contract version, request and trace IDs, tenant, trusted principal, project/workspace, session and agent identity, action name, canonical resource, content or operation digest, normalized attributes, idempotency key, deadline and optional proof references. Identity must be produced by a verified host or authentication boundary.

The **Decision** is one of ALLOW, DENY, REQUIRE_APPROVAL, UNAVAILABLE or INVALID. It includes policy ID/version, immutable basis references, safe explanation, expiry, limits and (for ALLOW) a broker-consumable authorization handle. An 'ask' is never a repackaged hard DENY.

The **EffectReceipt** records attempt ID, decision reference, broker principal, target digest, observed side effect, completion status, monotonic sequence and replay/idempotency linkage.

The **Event** carries typed name, version, correlation ID, causal parent, actor, tenant, subject, policy version, timestamp and hash-chain/audit verification metadata. Events are append-only; state materializations are derived and revocable.

Additional contracts: ModelCapabilities, ModelRequest, ModelEvent, CapabilityPack, TaskSpec, Artifact, Evidence and EvaluationRecord. Their fields contain no mandatory code repo, PR or source file. External agent mode does not require a ModelRequest. A pack may request tools but cannot authorize execution. JSON schema drafts live in [contracts](../contracts/) and are revised before any stable public API ships.

## 7. Policy lifecycle

1. Author a declarative policy manifest with unique ID, semantic version, scope, applicability/action type, precedence, decision class, failure posture, implementation binding, tests and owner.
2. Compile/validate with restricted grammar; reject conflicting hard-deny/allow semantics unless explicit deny precedence resolves them.
3. Run policy conflict analysis, example-based tests, static validation and differential evaluation against previous version.
4. Human or organization authority signs/publishes immutable bundle digest; never let an agent publish authority changes with a transcript string.
5. Evaluate normalized action deterministically; record which bundle and matched assertions produced decision.
6. Deploy with staged rollout, shadow compare, fail-safe rollback; invalidate or reevaluate pending approvals on policy change.
7. Expire/retire policies through a declared migration, not via stale path strings.

Advisory retrieval rules live in a **separate registry**. They can inform an agent but are not described as enforced unless bound to a real evaluator/executor and evidence exists.

## 8. State machine and approvals

Session status, review workflow and tool authority are separate state dimensions. A session's workflow phase is not permission to bypass an organization policy. Each state transition is an append-only event validated using an expected state revision. Concurrent attempts use optimistic concurrency and idempotent command handling; a stale request is REJECTED or RETRIED through an explicit conflict workflow, never silently overwrites.

Human approval flow:

1. Broker identifies a **REQUIRE_APPROVAL** action and produces an immutable approval request with canonical target/digest.
2. Trusted UI displays scope, plan or effect, risks, expiry and policy; the model may not synthesize the principal.
3. Authenticated human approves or denies using a separate trusted channel.
4. Operational store creates bound approval capability (principal, tenant, action, canonical target, digest, policy version, expiry, nonce, optional usage limit) atomically.
5. Broker checks policy and consumes approval exactly once or within declared use count at effect time.
6. Effect receipt records completion, failure or reconciliation state. Outage must not mint a synthetic approval.

## 9. Retrieval and Recall

JIT pipeline: normalize/query intent → resolve tenant/project permissions → candidate selection (BM25/vector) → graph-aware evidence enrichment (bounded, off critical permission path) → authority/staleness/conflict filter → deterministic rank and token budget → provenance-bearing response or abstain.

Do not throw away the V1 benchmarked ranking behavior without measuring replacement quality. Preserve golden queries, abstentions and negative isolation controls; evaluate lexical/vector/graph variants under equal corpora.

Memory types: user-approved durable preference, project decision, tool outcome, evidence observation, architecture constraint, ephemeral session note. Distinguish facts from claims and low-confidence inferences. Memory writes require scope, origin, TTL/retention, consent when needed and reconciliation rules. Graph relationships never themselves grant rights.

## 10. Implementation program and acceptance

| Milestone | Deliverable | Blocking gate |
|---|---|---|
| **M0 — audit and cross-domain contracts** | Source census, 52 hooks, 288-rule disposition, V1 corpus; TaskSpec/Artifact/Evidence; coding/research/review task fixtures; ModelAdapter/CapabilityPack schemas | Security inventory and provider/domain-neutral fixtures verified without real model credentials |
| **M1 — language bake-off** | Same Rust and Go executable action/policy core; independent stub-provider and cross-domain TaskSpec tests | Both run same permission/agent-independent fixtures; choose via ADR and reproducible perf/ops/security evidence |
| **M2 — contracts & kernel** | Typed schemas, pure state-transition reducer, policy resolution, decision provenance | Determinism, deny precedence, invalid/malformed input behavior, property tests |
| **M3 — authorization broker** | Trusted identity binding, effect mediation, canonical resource/digest, receipts | No effect without valid authorization; TOCTOU, replay and idempotency tests |
| **M4 — human approval** | Authenticated approval request/consume/revocation, UX and audit | Untrusted transcript cannot mint; stale, replayed or mismatched approval denied |
| **M5 — host and model adapters** | Claude Code host adapter first, generic host/ModelAdapter contracts, optional AgentRunner stub | External host needs no model API; two stub providers and coding/research/review fixtures work without policy divergence |
| **M6 — rule registry** | Policy manifests, compile/validate, conflict analyzer, rule-to-enforcer proof | 100% of enforced rules have fixtures, registered implementation and active enforcement point |
| **M7 — JIT retrieval** | Rebuilt/adapted ranker, scoped indexes, abstention, context budgets | Quality >= approved V1 golden set, predictable memory and p95; no cross-tenant data |
| **M8 — Recall & Neo4j** | Provenance schema, scoped memory, graph adapter, import/reconcile | Isolation, stale/contradictory evidence handling, round-trip and integrity tests |
| **M9 — operations** | Local installer, config, auth, backups, audit verification and OTel | Crash recovery, interrupted update, lost network and corrupted index drills |
| **M10 — enterprise profile** | Postgres HA configuration, tenant isolation, throttling and horizontal workers | Load/chaos/resource quota tests; explicit SLOs and total cost benchmark |
| **M11 — optional orchestration & plugins** | Writ-managed AgentRunner/CapPack activation as workload-driven, plus Temporal/Restate/WASM pilot only if required | Same task/evaluation schemas operate across providers/domains; isolated failure cannot alter kernel |
| **M12 — release & cutover** | Migration tooling, read-only V1 export, V2 import, dual comparison, rollback | Signed sign-off; safety tests green; no outstanding P0 defects; documented revert |

No milestone may claim 'done' because a document exists. Gate requires running code and recorded evidence.

## 11. Test and benchmark matrix

**Correctness:** example and property tests for state transitions; identical results across ports; conflict resolution; parser fuzzing; strict contract versioning; stable policy bundle hash. **Security:** forgery, cross-tenant read/write, replay, token swap, target path alias/symlink, environment variable injection, malformed UTF-8/large payload, process and network side-effect escapes. **Reliability:** crash before/after effect, duplicate event, retried approval, graph outage, broker restart, policy hot reload, reordered events. **Performance:** local p50/p95/p99, cold startup, peak RSS, events/sec, queue depth, graph query p95, index refresh cost and overall agent turn latency. **Cost:** CPU, memory, storage, tokens, graph licensing and operator toil. **Developer experience:** build times, incremental iteration, local reproduction and platform packaging.

Use independent workload generators; publish hardware, commit SHAs, corpora, concurrency and percentiles. Rust and Go must process the SAME payload sets and denial fixtures. A faster unsafe run does not pass.

## 12. Cutover and rollback

Create V2 separately from V1's active runtime. Export the V1 graph corpus, decisions and human-approved rule content read-only. Transform into versioned V2 records with lineage and conflict classification. Never import a V1 claim of 'mechanically enforced' as verified without re-binding it to an executable V2 validator. Shadow compare decisions without performing duplicate effects. Move a small nonprivileged workload, then privileged local workloads with hard rollback triggers. Rollback returns traffic to V1 only if its security posture is explicitly accepted; otherwise pause privileged execution and recover from receipts.

## 13. Delivery governance

Every milestone must include: architecture decision record, code artifact, test matrix, benchmark result, security evidence, rollback procedure, updated HTML diagram, and follow-up risk register. PR descriptions must clearly state **implemented**, **tested**, **assumed** and **not yet supported**. Defer exploratory services rather than shipping complex unused infrastructure.

## 14. Out-of-scope risks and unresolved questions

- Which host platforms expose complete execution mediation versus advisory-only hooks?
- What identity proof is available in local Claude Code events? Which requires a broker-launched process/container?
- How much V1 ranking fidelity matters across codebases and languages?
- Rust vs Go with realistic Neo4j and model-call dominant workloads?
- Where and how should an enterprise human approval UI authenticate principals?
- What storage and graph licenses are acceptable?
- What memory retention/consent policies are required per tenant?
- Which integrations need remote durable orchestration, and what is the measured cost?
- What is an acceptable local-personal-use degradation policy distinctly from enterprise strict enforcement?

Track these as explicit ADRs/experiments before promising features.

## 15. Definition of architecture-complete

Architecture is complete only when each module has: an API contract, caller, trust boundary, authoritative owner, storage responsibility, failure posture, upgrade strategy, threat-test suite and benchmark hypothesis. **Documentation complete is not implementation complete.**

## 16. Domain/platform requirements (normative extension)

[ADR-0006](../decisions/ADR-0006-model-and-domain-independence.md) establishes provider neutrality and general-purpose Capability Packs. The [model runtime](../architecture/model-agnostic-runtime.md), [Capability Pack design](../architecture/capability-packs.md), and [AgentRunner/evaluation](../architecture/agent-orchestration.md) are part of the V2 target architecture, not optional marketing claims. **ModelAdapter is optional for externally managed hosts**, and every provider feature requires negotiation. **Capability Packs never grant security rights**. The same TaskSpec, artifact, evidence and evaluation contracts must support software code review, noncode research and document review. Begin with the [M0 start checklist](M0-START-HERE.md). Real adapter support, pack activation and model quality remain unimplemented until passing tests.

## 17. Writ Control Center (normative extension)

[ADR-0007](../decisions/ADR-0007-control-center-management-plane.md) and [D-015](DECISION-LOG.md) add the Control Center as a first-class product requirement: **simple for ordinary users, powerful for advanced users, every important action explainable.** Specification: [docs/control-center](../control-center/README.md). It is a separate management plane, never in the decision path, never an authority, never required for headless operation, and not a prerequisite for M1.

| Milestone | Control Center deliverable |
|---|---|
| M2, M3 | Management contracts, service APIs, management authorization policy, audit query model, correlation, effect states, explanation service |
| M4 | Authenticated human approval experience (inbox, step-up, approve, deny, revoke) |
| M5 | Adapter status, integration diagnostics, coverage labels, host visibility |
| M6 | Rules, policies, contracts and Capability Pack management; enforcement labels |
| M7 | Knowledge-base administration and retrieval observability |
| M8 | Persistent memory and graph management |
| M9 | Unified local Control Center UI and operational administration; minimum usable Control Center for the first local beta complete |
| M10+ | Enterprise features where justified |

Details and gates: [implementation plan](../control-center/implementation-plan.md). Status: planned; no Control Center code exists.

## 18. Acceptance Gate Integrity (normative requirement; design proposed)

Owner direction 2026-11-06 ([D-016](DECISION-LOG.md)): Writ makes authorization **and acceptance** requirements enforceable outside the model's reasoning. Two mandatory boundaries in Governed Execution mode: the Action Authorization Gate (kernel plus broker, existing design) and the Acceptance and Promotion Gate (acceptance service plus transition service). The kernel stays the only permission authority. Specification and proposed design: [acceptance gate integrity](../architecture/acceptance-gate-integrity.md), [ADR-0008](../decisions/ADR-0008-acceptance-gate-integrity.md).

| Milestone | Added scope |
|---|---|
| M1 | Transition decisions, pure acceptance-binding check, CAS and single-use transition scenarios in both prototypes ([parity suite](M1-PARITY-SUITE.md)) |
| M2 | AcceptanceRecord, evaluator registry and runner attestation contracts; protected workflow states |
| M3 | Transition service; mechanical runner as sandboxed broker effect (moved from later milestones) |
| M4 | Signoff provenance; waivers as approvals |
| M5 | Mediated host operations proven; host-side transitions advisory |
| M6 | Signed acceptance policies; evaluator registry publication |
| M7, M8 | Evidence provenance and freshness; retrieval and memory never authoritative |
| M9 | Control Center acceptance traceability |
| M12 | Adversarial, integration and failure testing before production enforcement claims |

Sequencing changes are listed in section 13 of the specification. Nothing in this section is implemented.
