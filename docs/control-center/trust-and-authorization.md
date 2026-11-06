# Control Center: trust boundaries and authorization rules

**Status: PLANNED.** Design target; nothing implemented. Extends the [threat model](../security/threat-model.md) and [authorization and approvals](../architecture/authorization-and-approvals.md).

## Trust boundaries

| Component | Trust | Rationale |
|---|---|---|
| Browser UI | Untrusted client | Runs on a user device; all checks are server-side |
| CLI | Untrusted client with a local credential | Same API and checks as the UI |
| Management service | Trusted for management authorization only; **not** a decision or effect authority | Holds no ingress, permit or approval-signing keys; cannot mint grants or permits |
| Approval service | Trusted; owns grant creation | The management service forwards an authenticated human's approve or deny; the approval service verifies the step-up assertion and binds the grant (INV-06) |
| Policy registry | Trusted; owns bundle verification and publication | Rejects unsigned, failing or invariant-violating bundles regardless of caller |
| Kernel and broker | Trusted; unaware of the management plane | Pull signed bundles; no inbound management connection |
| Displayed content (knowledge, memory, request arguments, model output, logs) | Untrusted data | Rendered as escaped text; never interpreted as instructions or markup (CC-SEC-15) |

## Principal realms

Two realms, never interchangeable:

1. **Agent realm:** host adapters, agents, subagents, AgentRunner, ModelAdapters. They act through sealed ingress (D-010). Their credentials are refused by the management API (INV-28).
2. **Management realm:** authenticated humans and explicitly created management service accounts (for example a backup scheduler). Service accounts cannot approve requests or publish policy.

A model can never obtain a management credential through Writ: no API returns one, no tool exposes one, and management credentials are stored outside any workspace the broker can write.

## Roles

Built-in roles for the local beta. Custom roles are enterprise (CC-AD11).

| Role | Can | Cannot |
|---|---|---|
| Viewer | Read Overview, Activity, Rules, Knowledge, Memory, Integrations in assigned scopes | Any mutation |
| Operator | Viewer plus run reindex, refresh, backups, diagnostics; acknowledge warnings | Change policy, approve, manage users or keys |
| Approver | Decide approval requests routed to them, with step-up | Approve their own requests; change policy |
| Policy author | Create, edit, clone, validate, simulate drafts; register custom contracts | Publish or activate |
| Policy publisher | Publish, activate, deactivate (non-mandatory), roll back, retire | Edit mandatory invariants; publish without passing tests |
| Knowledge curator | Import, edit metadata, archive, delete eligible knowledge | Delete audit records; change retention policy |
| Memory steward | Correct, supersede, archive, delete eligible memories | Bypass consent or retention rules |
| Integration admin | Install and configure adapters and packs | Grant capabilities through a pack |
| Security auditor | Read all audit in scope, export with integrity proof | Any mutation except creating exports |
| Installation admin | Users, roles, keys (metadata), settings, backups, restores, migrations, tenants and projects | Read secret values; delete security audit records; disable audit |

Local single-user installs may hold several roles; conflicting-role actions (for example author and publisher) then require an explicit acknowledgment that is audited. Enterprise installs can make separation of duties mandatory (CC-R06).

## Authorization rules for administrative changes

1. **Deny by default.** Every management operation is evaluated against a management authorization policy, versioned and audited like any policy (INV-12).
2. **Scope intersection.** Effective scope = requested scope intersected with the principal's grants; never widened by UI state, URL parameters or search.
3. **Step-up for high-risk operations.** Approving, publishing or activating policy, rotating or revoking keys, restoring backups, deleting tenants, projects or knowledge sources, and changing roles require fresh authentication (default within 5 minutes).
4. **Mandatory invariants are not configurable.** Policies marked mandatory and the invariant set (INV-01..INV-31) cannot be deactivated, weakened or reordered through the management plane; the registry refuses such bundles even if the management service is compromised (CC-SEC-04).
5. **Every accepted mutation is audited before it takes effect.** Audit write failure refuses the mutation (CC-SEC-08).
6. **Versioned, not overwritten.** Policies, contracts, settings, adapter configurations and memories change by creating versions; published policy bundles are immutable (CC-SEC-12).
7. **No self-approval.** A principal cannot approve an approval request they originated or that was raised on their behalf.
8. **No secret values.** APIs return key ids, fingerprints and metadata only (INV-31).
9. **Bounded queries.** All reads bounded in rows, time and graph depth (CC-G02, INV-18).
10. **Packs never grant.** Enabling, upgrading or configuring a pack cannot change any permission outcome (INV-23, CC-SEC-16).

## Threats specific to the management plane

| Threat | Control | Test |
|---|---|---|
| Agent calls the management API to approve its own request | Agent-realm credentials refused; approvals need step-up human auth | CC-SEC-02, CC-SEC-07 |
| CSRF or DNS rebinding against the local UI | Loopback binding, Origin and Host checks, SameSite cookies, CSRF token | CC-SEC-06 |
| Stored XSS through knowledge, memory or request content | Untrusted rendering, strict Content Security Policy | CC-SEC-15 |
| Approver confused by misleading display | Display exact canonical target and digest; diff for content changes | CC-SEC-07 |
| Admin disables a mandatory policy | Registry-side invariant check | CC-SEC-04 |
| Admin hides activity by deleting knowledge | Audit records independent of content deletion | CC-SEC-09 |
| Management outage stops enforcement | Kernel independent of management plane | CC-SEC-01 |
| Secret leakage through diagnostics or logs | Redaction with canary-secret tests | CC-SEC-05 |
| Cross-tenant enumeration through search or deep links | Server-side scope filtering; uniform not-found responses | CC-SEC-03 |
