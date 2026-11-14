# Replaceable modules, plugins and orchestration

## Modular core

Modules follow ports-and-adapters (hexagonal) architecture. The kernel defines minimal ports:

~~~text
PolicyProvider.evaluate(context, action) -> DecisionBasis
StateStore.load(scope, expected_revision?) -> Snapshot
StateStore.commit(expected_revision, events) -> Revision
ApprovalStore.claim(grant, action_digest) -> ConsumeResult
BrokerExecutor.execute(permit, normalized_action) -> EffectReceipt
GraphPort.query(scope, bounded_pattern) -> EvidenceGraph
RetrievalPort.retrieve(scope, query, limits) -> EvidenceSet
MemoryPort.record(scope, source, retention) -> MemoryReceipt
AuditPort.append(scope, event) -> DurableSequence
WorkflowCoordinator.dispatch(workflow, revision) -> JobHandle
~~~

Every port has strict resource bounds, versioned schemas, cancellation, timeouts and failure categories. The domain kernel cannot dynamically import a provider SDK. An extension cannot request additional host capabilities simply by naming them.

## Package/plugin lifecycle

Manifest: plugin_id, version, API compatibility range, declared host capabilities, tenant scope, limits, data-access purpose, publisher signature, test vectors and health/readiness. Install verifies signature and admin policy. Disable and rollback require an explicit revocation procedure and cache invalidation.

**Trust tiers:**
- Built-in trusted module compiled with core, code-reviewed.
- Separate process sidecar with OS confinement; for connectors requiring native SDK.
- Sandboxed WASM module (deferred) with explicit imports, fuel/CPU/memory limits and no ambient filesystem/network rights.
- Remote integration behind authenticated API and rate limiting.

WASM does not magically confine host imports or prevent all side channels; the host capability API and OS boundaries remain authoritative.

## Durable workflows

Long-running review/approval/reconciliation flows may later use Temporal, Restate or another durable coordinator. The coordinator stores orchestration intent, backoff and rescheduling; all effects still call the broker. Do not add remote workflow dependency for synchronous local file permissions. Evaluate only after local M0–M9 functionality is correct.

## Multi-tenant execution

Per-tenant quotas, admission control, max concurrency, prioritized queues, backpressure, policy bundle namespace and audit partition. Local installation has one explicit tenant and local principal, but keeps the same wire and storage contracts. Enterprise profile does not assume Neo4j alone provides isolation.

## Replaceability tests

Every adapter must pass the same contract suite, not merely implement a method with the same name. Change implementations behind stable API versions, compare verdicts in shadow mode, and run failure injection for extension crash/unavailability. None may alter security decisions on timeout.

## Capability Packs versus executable plugins

A Capability Pack is primarily a signed, declarative **domain workflow** (task types, artifacts, evidence requirements, rubrics, tool requirements and memory scopes). It is not automatically executable or authorized. This is how research and noncode review coexist with coding in one kernel. An executable WASM/sidecar extension is a separate trust tier with explicit broker capabilities. An optional ModelAdapter/AgentRunner is not needed for an external host. See [packs](capability-packs.md) and [managed runner](agent-orchestration.md).
