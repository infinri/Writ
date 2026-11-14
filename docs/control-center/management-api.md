# Control Center: management API architecture

**Status: PLANNED.** Design target; no service or API exists. Decision record: [ADR-0007](../decisions/ADR-0007-control-center-management-plane.md).

## Components

~~~mermaid
flowchart LR
  UI[Control Center web UI, optional] --> MS[Management service]
  CLI[writ CLI admin commands] --> MS
  MS -->|read-only views| Audit[(Security audit store)]
  MS -->|read-only views| Ledger[(Operational ledger)]
  MS -->|read| Telemetry[(Metrics, traces, logs)]
  MS -->|publish request| Registry[Policy and contract registry]
  MS -->|approve or deny request| Approval[Approval service]
  MS --> Knowledge[Knowledge service]
  MS --> Memory[Memory service]
  MS --> AdminSvc[Admin services: tenants, users, keys, backups]
  Kernel[Kernel] -->|pulls signed bundles| Registry
  Kernel -->|emits| Audit
  Kernel -->|emits| Telemetry
~~~

The management service is a separate process. The kernel has **no dependency** on it: the kernel pulls signed bundles from the registry on its own schedule and never waits on, calls or receives pushes from the management service (INV-27). The management service holds no ingress seal key, broker permit key or approval signing key.

## API conventions

| Concern | Rule |
|---|---|
| Versioning | Path-versioned (`/manage/v1/...`); additive changes within a major version; breaking changes need a new major and a deprecation window of at least one minor release |
| Contracts | Request and response schemas in `docs/contracts/management/` (created at M2), validated in CI with positive and negative fixtures like the core contracts |
| Authentication | Every request authenticated as a **human or service principal of the management realm**; agent and host-adapter credentials are refused (INV-28, CC-SEC-02) |
| Authorization | Evaluated by the management authorization policy ([trust and authorization](trust-and-authorization.md)); deny by default |
| Concurrency | Mutations carry the expected revision (`If-Match`); stale revisions return `409 REVISION_CONFLICT` (INV-17) |
| Idempotency | Mutations accept an idempotency key; a replay with a different body returns `409 IDEMPOTENCY_CONFLICT` |
| Preview | Every mutation supports `preview=true`, returning the computed effect without applying it (CC-G08) |
| Audit | Every accepted mutation returns the id of its security audit event; if the audit record cannot be written, the mutation is refused (CC-SEC-08) |
| Pagination | Cursor-based; default 50, maximum 500; server-side query timeout 5 s by default (CC-G02) |
| Long operations | Imports, reindex, backup, restore, migrations and deletion reconciliation run as jobs with status, progress, cancel and result resources |
| Errors | Stable machine codes, user message, cause and recovery step; no stack traces (CC-G11) |
| Secrets | No response field ever carries a secret value; keys are referenced by id and fingerprint only (INV-31) |
| Rate limits | Per principal and per endpoint class; exceeding returns `429` with retry time |

## Resource groups

| Group | Example resources | First milestone |
|---|---|---|
| `health` | components, checks | M9 (M5 for adapters) |
| `audit` | events, decisions, effects, approvals, timelines, explanations, exports | M3 |
| `approvals` | requests, decisions on requests, grants (metadata), revocations | M4 |
| `policies` | bundles, drafts, versions, simulations, publications, activations | M6 |
| `contracts` | schemas, versions, validations, registrations, deprecations | M6 |
| `packs` | packs, verifications, enablement | M6 |
| `integrations` | adapters, coverage, configurations, compatibility | M5 |
| `knowledge` | sources, imports, documents, chunks, index health, retrieval inspections | M7 |
| `memory` | memories, relationships, corrections, consent, retention | M8 |
| `admin` | tenants, projects, users, roles, keys (metadata), settings, backups, restores, migrations, logging, diagnostics | M2 (tenancy contracts), M9 |
| `jobs` | jobs | M7 |

## Essential operations

These must be available through both the `writ` CLI and the API, so headless installations never need the UI (CC-G14):

1. Health and status of all components.
2. Search and show audit events, decisions, effects and their explanations; export with integrity proof; verify an export offline.
3. List, show, approve, deny and revoke approval requests (approval still requires step-up human authentication).
4. Validate, simulate, publish, activate, deactivate and roll back policy bundles.
5. Validate an example against a contract.
6. Install, verify, enable and disable Capability Packs; show adapter coverage.
7. Import, reindex, archive and delete knowledge sources; show reconciliation reports.
8. Show, supersede, archive and delete memories.
9. Create and archive projects; manage users and roles; rotate and revoke keys.
10. Backup, restore, run migrations, produce a redacted diagnostics bundle.

## Hot-path isolation

- The kernel decision path imports no management module and opens no connection to the management service.
- Audit and telemetry are written by the kernel and broker into their own stores; the management service reads them through read-only views or replicas.
- Management queries run with bounded time, rows and memory and, for SQLite, on a separate read connection so they cannot hold the write lock the broker needs.
- Acceptance: kill or overload the management service while the parity and broker suites run; decisions are identical and kernel p95 stays within run-to-run variance (CC-SEC-01).

## Transport

- **Local:** the service listens on loopback only (or a Unix domain socket), on a port chosen at install. The UI is served by the same service. Browser sessions use HttpOnly, Secure, SameSite=Strict cookies plus a CSRF token, and the service checks `Origin` and `Host` (CC-SEC-06). The CLI authenticates with a local credential file readable only by the installing user.
- **Enterprise (M10):** TLS, OIDC or SAML sign-in, service accounts for automation, network policy restricting the management port.
