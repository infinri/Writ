# Execution kernel, state and effect protocol

## Normalized action life cycle

1. Host adapter receives event and verifies host identity / peer origin.
2. Canonicalizer maps tool call to a **declared action**, resource scope and content/operation digest.
3. Kernel loads relevant state revision and immutable policy version.
4. Pure evaluator returns ALLOW, DENY, REQUIRE_APPROVAL, INVALID, or UNAVAILABLE with structured reason.
5. For ALLOW, broker obtains a short-lived bound authorization capability and revalidates it at execution time.
6. Broker atomically records an effect attempt with idempotency key; checks replay and limit state.
7. Effect adapter executes within granted capability/sandbox.
8. Broker persists observed receipt, reconciles ambiguous timeouts, and emits audit-linked event.
9. Adapter maps result into the host-specific response; context injection is a separate advisory operation.

The kernel must not execute a file write while computing a verdict.

~~~mermaid
sequenceDiagram
  participant H as Host adapter
  participant K as Kernel
  participant P as Policy
  participant O as Operational store
  participant B as Broker
  participant E as Effect adapter
  H->>K: ActionRequest + verified principal
  K->>P: Evaluate immutable bundle
  K->>O: Read state revision
  K-->>H: Decision + permit reference
  H->>B: Execute(action, permit)
  B->>O: Idempotency claim + permit consume
  B->>E: Execute within capability
  E-->>B: Observed effect
  B->>O: Receipt/compensation state
  B-->>H: EffectReceipt
~~~

## Reducer and state transitions

State is materialized by reducing immutable ordered events. Commands carry expected revision, subject and idempotency key. Concurrent changes use compare-and-swap. Missing/out-of-order causal predecessors return conflict requiring reconciliation. Auth policy is not a property of a workflow phase, so selecting 'conversation' or 'prototype' never bypasses organization restrictions.

Design states:

- **Session:** initiated, active, paused, ended.
- **Workflow:** planning, approval-pending, executing, verifying, completed, cancelled; optional project-specific stages.
- **Approval:** requested, granted, denied, expired, revoked, consumed.
- **Effect:** proposed, permitted, reserved, dispatched, observed-success, observed-failure, unknown-reconcile, compensated.
- **Policy bundle:** draft, verified, published, superseded, revoked.

Only explicit transition commands with evidence and authority can change protected states. State revision is part of the decision evidence.

## Idempotency and outages

Duplicate action with same request + digest returns existing receipt if effect completed. Same key with different action digest is INVALID. Crash after dispatch but before receipt creates UNKNOWN and requires effect-specific reconciliation; do not blindly retry irreversible actions. Broker keeps durable authorization and effect receipts even if telemetry sink fails. Read-only advisory queries may continue on graph outage; privileged actions reject when required policy or receipt storage is unavailable.

## Capability limitations

Host hooks may intercept specific named tools but cannot prove all filesystem/network/process effects. A true **broker-enforced profile** requires all permitted effect routes to be broker-owned, or an independently verified host sandbox. A **hook-advisory profile** must label unsupported or unmediated tool paths. This difference is displayed in product UI and audit events.
