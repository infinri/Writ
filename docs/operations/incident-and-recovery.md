# Incident response, fail-safe handling and recovery

## Primary goal

Preserve authority and causal effect evidence through crashes, timeouts and partial outage. An unavailable monitoring stream must not cause authorization to open. A graph outage may remove advisory context but must not rewrite policy.

## Priority triage

- **Policy signature/identity failure:** stop privileged execution; reject new permits; retain previous verified bundle only when its validity and rollback rules permit.
- **Broker failure:** pause broker-owned effects; restore state and reconcile effects already dispatched.
- **Approval store failure:** no new grant/consumption; deny or return unavailable, never infer approval from transcripts.
- **Postgres/SQLite corruption:** isolate instance, restore backup, verify event chain and grant use counts; do not reissue consumed approvals automatically.
- **Neo4j/retrieval failure:** abstain from context; policy path remains independent.
- **Out-of-order/duplicated event:** compare idempotency key, action digest and expected revision; reject mismatch.
- **Host adapter gaps:** mark actions unsupported and block governed profile rather than guessing success.

## Ambiguous effects

An action dispatched externally before broker receipt is **UNKNOWN**. Query the external system by trusted idempotency/operation identifier when available. For irreversible actions (push, publish, payment, deletion), never retry just because response timed out. A human may be required to reconcile the external state.

## Forensics

Capture revision, trace/request IDs, tenant and principal reference, policy bundle SHA, state revision, approval-grant digest (never plaintext), broker attempt and external operation ID, before/after receipt status and audit-chain validation. Preserve evidence without copying secrets across tenants.

## Recovery acceptance

Restored system must independently verify active policy, approval counts, open effect intents, event sequence and backup freshness. Run negative authorization control after recovery, then gradually reopen controlled capabilities. A postmortem records root cause, blast radius, time to detection, unmediated paths, test addition and required ADR.
