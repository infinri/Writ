# Control Center: audit and observability model

**Status: PLANNED.** Extends [observability](../architecture/observability.md). Nothing here is implemented.

## Four data classes

| Class | Examples | Writer | Mutability | Retention and deletion | Control Center access |
|---|---|---|---|---|---|
| **Security audit records** | Decisions, approval requests and grants, permit issue and consume, effect intents and receipts, policy publish and activation, management mutations, key lifecycle | Kernel, broker, approval service, registry, management service, inside the governing transaction | Append-only, hash-linked; never edited | Retained for the configured minimum (default 1 year local, policy-defined in enterprise). Removed only by retention expiry of whole segments, recorded with a signed tombstone; legal hold blocks expiry. **Never removed by knowledge or memory deletion** | Read, search, explain, export with integrity proof |
| **Operational logs** | Component logs, warnings, job output | Every component | Append; rotated | Size- and age-based rotation (default 14 days local) | Read, search, filter; levels configurable except security audit |
| **Metrics and traces** | Latency histograms, counters, OpenTelemetry spans | Every component, best effort | Aggregated, sampled | Short retention (default 7 days local); loss tolerated | Dashboards, trace view |
| **Knowledge and memory content** | Source documents, chunks, embeddings, graph nodes and edges, memories | Knowledge and memory services | Versioned; editable; deletable when eligible | Per source and per memory retention, consent and project rules ([lifecycle semantics](lifecycle-semantics.md)) | Browse, edit, archive, delete |

Rules:

- Telemetry failure never blocks a security audit write or changes a decision; security audit failure on a privileged path blocks the effect (existing observability rule).
- Security audit records carry **digests and references, not content**. A decision references a knowledge chunk by id and digest, so deleting the chunk leaves an intact audit record that says the referenced content was deleted, when and by whom.
- Personal data in audit is limited to principal ids. Where erasure law applies, principal display data lives outside the audit chain and is erased there; the audit keeps a pseudonymous id.

## Correlation

Every record carries `trace_id`, `request_id`, `tenant_id`, `project_id` and, where applicable, `attempt_id`, `grant_id`, `permit_id`, `policy_sha` and `state_revision`. The timeline view joins on these keys in causal order (CC-A03). Traces may be sampled; the security audit chain is never sampled, so a timeline is complete even when spans are missing, and the UI marks missing spans as "trace not sampled" rather than hiding the gap.

## Attempted versus executed

| Effect state | Meaning | Display |
|---|---|---|
| INTENT | Broker reserved the attempt | Attempted, not executed |
| DISPATCHED | Effect adapter called; no receipt yet | In progress, or UNKNOWN after timeout |
| COMPLETED | Receipt recorded | Executed |
| FAILED | Receipt records failure | Attempted, failed |
| UNKNOWN_RECONCILE | Crash or timeout between dispatch and receipt | Needs reconciliation; never shown as executed |
| REFUSED | Broker refused before dispatch | Not executed, with refusal code |

Unmediated operations are shown separately, from host coverage data and any host-reported events, labelled UNMEDIATED and excluded from governed totals (CC-A05).

## Why did this happen?

The explanation service builds a deterministic explanation from recorded records only (CC-G06):

| Subject | Explanation inputs | Output |
|---|---|---|
| Decision | Decision record, policy bundle sha and version, matched rule ids and their text, request attributes used, approval state, deadline | Status, the deciding rule and its precedence, what would change the outcome (for example "an approval from role X"), links |
| Effect | Intent, permit, grant, receipt, reconciliation | Which authorization enabled it, what was executed, any mismatch |
| Failure | Error record, component health at that time | Cause, affected component, recovery step |
| UNAVAILABLE | Health and dependency records | Which dependency was unavailable and the fail-closed posture applied |
| Retrieval result | Retrieval inspection record | Filters, ranking factors, abstention reason |

Constraints: no model call is required to produce an explanation; model-generated summaries may be offered later only as a separately labelled, optional aid and never as the cause (CC-SEC-11). An explanation for a DENY never reveals content from a scope the viewer cannot access.

## Bounded operational data

All audit and log queries are paginated and time-bounded (CC-G02). Exports run as jobs. Dashboards read pre-aggregated metrics, never raw audit scans.
