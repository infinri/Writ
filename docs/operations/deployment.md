# Deployment profiles and operations

## Supported deployment profiles (target only)

**Local:** one trusted operator + explicitly scoped project, single small kernel/broker process, secure IPC, SQLite operational ledger and optional local indexing; Neo4j may be connected for intelligence but is not required for every write decision. No required remote queue, Kubernetes or workflow engine.

**Team:** isolated authorized workspace, signed policy bundles, authenticated host adapters, centralized Neo4j (optional knowledge service), Postgres operational store, service identity and encrypted backups. Shared policy with per-project overrides respecting deny precedence.

**Enterprise:** per-tenant logical or physical boundaries appropriate to threat model; authn/authz, quotas, secure API gateway, Postgres HA, graph lifecycle, external key management, audit export, horizontal brokers/workers and optional durable coordinator. HA does not mean blindly deploying every data service; assess cost/licensing and failover tests.

## Deployment topology

~~~text
host adapter --> local broker/kernel --> host-governed effects
                       |
                       +--> local policy bundle, state cache
                       |
                       +--> optional authenticated enterprise gateway
                                        |
                            +-----------+-----------+
                            |                       |
                         Postgres                Neo4j
                     approvals/receipts      graph intelligence
                            |
                      durable events
~~~

## Security

Local UDS or named pipe requires peer validation; localhost without authentication is not a generic enterprise trust boundary. Use least-privilege service users, secure data directories, cryptographically verified policy bundles, secret rotation, network restriction and signed release artifacts. Secrets must never be in audit payloads.

## Schema migration and upgrade

Expand/contract database migrations with dual-compatible versions; backup; dry-run; verify constraints and tenant scope; roll out new runtime; monitor; retire old schema only after confirmed cutover. Policy bundle rollback must consider outstanding approvals and effect records, not just switch a file.

## Operating limits

Bound per-session memory, index generations, graph fan-out, result budgets, concurrent actions, request size, queue depth and retry budgets. Production monitoring uses p95/p99 rather than mean only. Keep local permission path available with last verified policy snapshot while dependent graph services are down; if authorization store cannot be trusted, stop new effects.

## Backup and DR

Test backup and restore of operational state and graph separately, preserving audit chain anchors, encryption metadata, policy version pointers and effect reconciliation state. Recovery objectives remain OPEN until enterprise requirements are specified and tested.

## Licensing and vendor costs

Evaluate Neo4j Community vs Enterprise/Aura for clustering, HA, operational support and isolation; policy/wasm/workflow vendor licenses and associated operational toil. Avoid promising enterprise graph clustering with a local Community container.

See [incident response](incident-and-recovery.md) and [master plan](../implementation/MASTER-IMPLEMENTATION-PLAN.md).
