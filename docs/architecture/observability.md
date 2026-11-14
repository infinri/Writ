# Observability, evidence and audit contracts

**Goal:** every authorization and effect is explainable without turning observability into a permission source.

## Two distinct channels

**Operational audit (durable):** request, principal proof reference, policy bundle SHA, state revision, decision, approval grant reference, effect intent, receipt and reconciliation outcome. Must be written/committed as part of the relevant authorization/effect transaction; audit unavailability on privileged path is a blocking error.

**Diagnostic telemetry (best effort):** spans, host latency, retrieval scores, memory hit ratios, module health and benchmark counters. Telemetry failure must not convert a DENY into ALLOW, leak secrets or block safe read-only operations unnecessarily.

## Minimum trace

~~~text
host_event_received
  -> adapter.normalized
  -> policy.bundle_resolved
  -> kernel.decision (status, policy_version, rule_ids)
  -> approval.requested / grant.consumed (if applicable)
  -> broker.intent_reserved
  -> adapter.effect_dispatched
  -> broker.receipt_recorded OR broker.reconciliation_required
  -> host.response_sent
~~~

Common attributes: trace_id, request_id, tenant_id, project_id, action.kind, resource.hash (not raw secrets), policy_sha, state_revision, decision.status, broker.attempt_id. Never log grant plaintext, full credentials, secret-bearing source contents, private prompts by default or cross-tenant PII into common metrics.

## Metrics and SLO partitions

- Kernel deterministic decision p50/p95/p99, reject rates, policy cache hit.
- Adapter invocation and normalization failures, unsupported capability count.
- Broker effect success/failure/unknown, idempotency collision, receipt backlog.
- Approval pending/expired/consumed/revoked, unauthorized-replay refusal count.
- Retrieval query latency/quality/abstention, index refresh generation and memory RSS.
- Graph query latency, cache invalidation, neighbor scope filtering, cross-tenant deny.
- Crash/restart recovery duration, audit chain continuity and operator intervention.

## Incident procedure

Stop new privileged effects on loss of trusted policy, approval ledger or broker. Inspect durable effect intents, reconcile UNKNOWN before retry, invalidate compromised grants, verify audit linkage and restore unaffected tenant operations. See [incident and recovery](../operations/incident-and-recovery.md).

## Evidence levels

Claim as **implemented** only with source path + commit; **verified** only with reproducible test/log; **benchmarked** only with hardware/workload version and results; **planned** when only docs exist. Every website diagram displays planning status until verified.
